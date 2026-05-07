from __future__ import annotations

import sqlite3
import tempfile
import unittest
import datetime as dt
import os
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from bilder.cli import build_parser, main
from bilder.db import DB_FILENAME
from bilder.importer import safe_copy
from bilder.media import sha256_file
from bilder.program_state import PROGRAM_DB_FILENAME
from bilder.target_lock import LOCK_FILENAME
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
                str(imported.resolve()),
                str(imported.resolve()),
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


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.program_root_tempdir = tempfile.TemporaryDirectory()
        self.program_root = Path(self.program_root_tempdir.name)
        self.program_root_patcher = patch("bilder.cli.program_repo_root", return_value=self.program_root)
        self.program_root_patcher.start()

    def tearDown(self) -> None:
        self.program_root_patcher.stop()
        self.program_root_tempdir.cleanup()

    def test_main_without_arguments_shows_help(self) -> None:
        code, stdout, stderr = capture_cli([])

        self.assertEqual(code, 0)
        self.assertIn("usage: bildebank [-h] [--version] <kommando> [<args>]", stdout)
        self.assertIn("Bildebank 0.2.0", stdout)
        self.assertIn("Vanlige kommandoer:", stdout)
        self.assertIn("bildebank <kommando> -h", stdout)
        self.assertEqual(stderr, "")

    def test_main_help_groups_commands_by_user_task(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("usage: bildebank [-h] [--version] <kommando> [<args>]", stdout)
        self.assertIn("Bildebank 0.2.0", stdout)
        self.assertNotIn("--target", stdout)
        self.assertIn("Vanlige kommandoer:", stdout)
        self.assertIn("kom i gang\n   create", stdout)
        self.assertIn("se og kontrollere samlingen\n   status", stdout)
        self.assertIn("finne ting som bør kontrolleres\n   conflicts", stdout)
        self.assertIn("programmet\n   where-is", stdout)
        self.assertIn("bildebank <kommando> -h", stdout)
        self.assertNotIn("{create,add,import", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

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

    def test_target_command_is_not_available(self) -> None:
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["target", "."])

    def test_old_conflict_and_remove_commands_are_not_available(self) -> None:
        for command in ("list-name-conflicts", "show-name-conflict", "delete", "list-deleted"):
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
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            self.assertTrue(imported.exists())
            self.assertEqual(imported.read_bytes(), b"image-one")

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 1)
                self.assertEqual(
                    conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0],
                    "3",
                )
                file_columns = {row[1] for row in conn.execute("PRAGMA table_info(files)")}
                self.assertNotIn("source_id", file_columns)
                self.assertNotIn("source_path", file_columns)
                self.assertNotIn("source_path_key", file_columns)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM file_sources WHERE kind = 'duplicate'").fetchone()[0], 0
                )
            finally:
                conn.close()

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

    def test_where_is_works_without_target(self) -> None:
        code, stdout, stderr = capture_cli(["where-is"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("Bildebank-program:", stdout)
        self.assertIn("Ingen registrert ennå.", stdout)

    def test_rejects_target_inside_program_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "bildebank"
            target = repo / "samling"
            repo.mkdir()

            with patch("bilder.cli.program_repo_root", return_value=repo.resolve()):
                code, stdout, stderr = capture_cli(["create", str(target)])

            self.assertEqual(code, 1)
            self.assertIn("Målmappen kan ikke ligge inni programmappen", stderr)
            self.assertFalse((target / DB_FILENAME).exists())

    def test_update_runs_update_script_without_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            update_script = repo / "update.ps1"
            update_script.write_text("# update\n", encoding="utf-8")

            with (
                patch("bilder.cli.sys.platform", "win32"),
                patch("bilder.cli.program_repo_root", return_value=repo),
                patch("bilder.cli.subprocess.run") as subprocess_run,
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
                patch("bilder.cli.sys.platform", "linux"),
                patch("bilder.cli.program_repo_root", return_value=repo),
                patch("bilder.cli.subprocess.run") as subprocess_run,
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
                patch("bilder.cli.sys.platform", "linux"),
                patch("bilder.cli.program_repo_root", return_value=repo),
                patch("bilder.cli.shutil.which", return_value="/usr/bin/python3.13"),
                patch("bilder.cli.subprocess.run") as subprocess_run,
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
                patch("bilder.cli.sys.platform", "win32"),
                patch("bilder.cli.program_repo_root", return_value=repo),
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
                patch("bilder.cli.sys.platform", "win32"),
                patch("bilder.cli.program_repo_root", return_value=repo),
                patch("bilder.cli.subprocess.run", side_effect=FileNotFoundError),
            ):
                code, stdout, stderr = capture_cli(["update"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Fant ikke PowerShell", stderr)

    def test_add_accepts_path_with_accidental_trailing_quote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "add", str(source) + '"']),
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
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"

            code, stdout, stderr = capture_cli(["--target", str(target), "show-source", str(imported)])

            self.assertEqual(code, 0, stderr)
            self.assertIn(f"Målfil: {imported.resolve()}", stdout)
            self.assertIn(f"Kildefil: {source_file.resolve()}", stdout)
            self.assertIn("Kildefil finnes: ja", stdout)
            self.assertIn("Kilde-id: 1", stdout)
            self.assertIn("Kildetype: directory", stdout)
            self.assertIn(f"Kilde: {source.resolve()}", stdout)
            self.assertIn("Originalt filnavn: IMG_20240102.jpg", stdout)
            self.assertIn("Lagret filnavn: IMG_20240102.jpg", stdout)
            self.assertIn("Dato: 2024-01-02 (filename)", stdout)
            self.assertIn("SHA-256:", stdout)

    def test_remove_moves_file_marks_database_and_hides_from_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

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
                self.assertEqual(Path(row["target_path"]), deleted.resolve())
                self.assertEqual(Path(row["deleted_original_target_path"]), imported.resolve())
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

    def test_import_dry_run_lists_files_without_database_or_copy_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "IMG_20240102.jpg"
            source_file.write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                commands_before = conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0]
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "import", "--dry-run", "--quiet"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("IMPORT\t2024-01-02\tfilename", stdout)
            self.assertIn(str(source_file.resolve()), stdout)
            self.assertIn(str((target / "2024" / "01" / "IMG_20240102.jpg").resolve()), stdout)
            self.assertIn("importert=1", stdout)
            self.assertFalse((target / "2024").exists())

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)
                self.assertIsNone(conn.execute("SELECT imported_at FROM sources").fetchone()[0])
                commands_after = conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0]
                self.assertEqual(commands_after, commands_before)
            finally:
                conn.close()

    def test_import_dry_run_log_file_writes_list_to_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")
            log_file = root / "dry-run.txt"

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "import",
                    "--dry-run",
                    "--quiet",
                    "--log-file",
                    str(log_file),
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev dry-run importliste", stdout)
            self.assertNotIn("IMG_20240102.jpg\t->", stdout)
            content = log_file.read_text(encoding="utf-8")
            self.assertIn("IMPORT\t2024-01-02\tfilename", content)
            self.assertIn("IMG_20240102.jpg", content)

    def test_import_stops_when_target_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            lock_path = target / LOCK_FILENAME
            lock_path.write_text("command=import\npid=123\n", encoding="utf-8")

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "import", "--quiet"]
            )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Målmappen er låst", stderr)
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
            self.assertEqual(run_cli(["--target", str(target), "add", str(source1)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source2)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 2)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM file_sources WHERE kind = 'duplicate'").fetchone()[0], 1
                )
                self.assertFalse(
                    conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'duplicate_findings'"
                    ).fetchone()
                )
            finally:
                conn.close()

    def test_remove_source_removes_pending_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "remove-source", str(source)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Fjernet kilde", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)
            finally:
                conn.close()

    def test_remove_source_rejects_active_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "remove-source", str(source)]
            )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("bildebank unimport", stderr)

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
            self.assertEqual(run_cli(["--target", str(target), "add", str(source1)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source2)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            with patch("builtins.input", return_value="ja, det vil jeg"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", str(source2)]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Filer som fjernes fra aktiv samling: 0", stdout)
            self.assertIn("Filer som blir liggende fordi de også finnes i andre kilder: 1", stdout)
            self.assertTrue(imported.exists())

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 1)
                status = conn.execute(
                    "SELECT status, imported_at FROM sources WHERE path = ?",
                    (str(source2.resolve()),),
                ).fetchone()
                self.assertEqual(status[0], "pending")
                self.assertIsNone(status[1])
            finally:
                conn.close()

            self.assertEqual(
                run_cli(["--target", str(target), "remove-source", str(source2)]), 0
            )

    def test_unimport_only_source_removes_target_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            self.assertTrue(imported.exists())

            with patch("builtins.input", return_value="ja, det vil jeg"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", str(source)]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Filer som fjernes fra aktiv samling: 1", stdout)
            self.assertIn("Kilden er satt tilbake til pending.", stdout)
            self.assertIn(f'bildebank remove-source "{source.resolve()}"', stdout)
            self.assertFalse(imported.exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_sources").fetchone()[0], 0)
            finally:
                conn.close()

    def test_unimport_aborts_without_exact_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)
            imported = target / "2024" / "01" / "IMG_20240102.jpg"

            with patch("builtins.input", return_value="ja"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", str(source)]
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
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)
            source_file.write_bytes(b"changed")

            with patch("builtins.input", return_value="ja, det vil jeg"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", str(source)]
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
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
            self.assertEqual(run_cli(["--target", str(target), "add", str(child)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(parent)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            with patch("builtins.input", return_value="ja, det vil jeg"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", str(child)]
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Kan ikke unimportere en superseded kilde", stderr)
            self.assertTrue((target / "2006" / "10" / "IMG_20061003.jpg").exists())

    def test_remove_source_dry_run_reports_superseded_reassignment_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            parent = root / "Bilder"
            child = parent / "2006"
            child.mkdir(parents=True)
            (child / "IMG_20061003.jpg").write_bytes(b"child")
            (parent / "IMG_20070104.jpg").write_bytes(b"parent")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(child)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(parent)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "remove-source", "--dry-run", str(child)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Rader som omregistreres til overordnet kilde: 1", stdout)
            self.assertIn("Dry-run: ingen endringer er gjort.", stdout)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 2)
                self.assertEqual(
                    conn.execute(
                        "SELECT status FROM sources WHERE path = ?",
                        (str(child.resolve()),),
                    ).fetchone()[0],
                    "superseded",
                )
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0], 5)
            finally:
                conn.close()

    def test_remove_source_reassigns_superseded_source_to_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            parent = root / "Bilder"
            child = parent / "2006"
            child.mkdir(parents=True)
            child_file = child / "IMG_20061003.jpg"
            parent_file = parent / "IMG_20070104.jpg"
            child_file.write_bytes(b"child")
            parent_file.write_bytes(b"parent")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(child)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(parent)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "remove-source", str(child)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Fjernet superseded kilde", stdout)
            self.assertTrue((target / "2006" / "10" / "IMG_20061003.jpg").exists())
            self.assertTrue((target / "2007" / "01" / "IMG_20070104.jpg").exists())

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                sources = conn.execute("SELECT id, path FROM sources").fetchall()
                self.assertEqual(len(sources), 1)
                self.assertEqual(sources[0][1], str(parent.resolve()))
                file_sources = conn.execute(
                    "SELECT source_id, source_path FROM file_sources ORDER BY source_path"
                ).fetchall()
                self.assertEqual(len(file_sources), 2)
                self.assertEqual({row[0] for row in file_sources}, {sources[0][0]})
                self.assertIn(str(child_file.resolve()), {row[1] for row in file_sources})
                self.assertIn(str(parent_file.resolve()), {row[1] for row in file_sources})
            finally:
                conn.close()

    def test_migrate_v2_removes_legacy_source_fk_before_remove_superseded_source(self) -> None:
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
                        str(child_target_file.resolve()),
                        str(child_target_file.resolve()),
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
                        str(parent_target_file.resolve()),
                        str(parent_target_file.resolve()),
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
            self.assertIn("Ny schema_version: 3", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("select value from meta where key = 'schema_version'").fetchone()[0],
                    "3",
                )
                file_columns = {row[1] for row in conn.execute("pragma table_info(files)")}
                self.assertFalse({"source_id", "source_path", "source_path_key"} & file_columns)
                self.assertEqual(conn.execute("pragma foreign_key_list(errors)").fetchall(), [])
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "remove-source", str(child)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Fjernet superseded kilde", stdout)
            self.assertTrue(child_target_file.exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("select count(*) from sources").fetchone()[0], 1)
                self.assertEqual(
                    conn.execute(
                        "select count(*) from file_sources where source_id = ?",
                        (parent_id,),
                    ).fetchone()[0],
                    2,
                )
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
            self.assertEqual(run_cli(["--target", str(target), "add", str(source1)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source2)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            code, stdout, stderr = capture_cli(["--target", str(target), "show-source", str(imported)])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Kildefiler:", stdout)
            self.assertIn(f"- imported: {first.resolve()}", stdout)
            self.assertIn(f"- duplicate: {duplicate.resolve()}", stdout)

    def test_status_counts_media_types_and_date_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")
            (source / "video.mp4").write_bytes(minimal_mp4_with_creation_date(dt.date(2010, 7, 8)))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "status"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Totalt: 2", stdout)
            self.assertIn("Bilder: 1", stdout)
            self.assertIn("Videoer: 1", stdout)
            self.assertIn("  metadata: 1", stdout)
            self.assertIn("  filename: 1", stdout)
            self.assertIn("  mtime: 0", stdout)

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
            self.assertEqual(run_cli(["--target", str(target), "add", str(child)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(parent)]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "import", "--quiet"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("dekket=1", stdout)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 2)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM file_sources WHERE kind = 'duplicate'").fetchone()[0], 0
                )
                statuses = conn.execute(
                    "SELECT path, status, superseded_by_source_id FROM sources ORDER BY id"
                ).fetchall()
                self.assertEqual(statuses[0][1], "superseded")
                self.assertEqual(statuses[0][2], 2)
                self.assertEqual(statuses[1][1], "imported")
            finally:
                conn.close()

    def test_rejects_child_source_after_parent_is_registered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            parent = root / "Bilder"
            child = parent / "2007"
            child.mkdir(parents=True)

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(parent)]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "add", str(child)])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("allerede registrert kildemappe", stderr)

    def test_rejects_superseded_child_source_added_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            parent = root / "Bilder"
            child = parent / "2006"
            child.mkdir(parents=True)
            (child / "IMG_20061003.jpg").write_bytes(b"child")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(child)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(parent)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "add", str(child)])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Kildemappen er allerede registrert", stderr)

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
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            self.assertTrue((target / "2024" / "01" / "IMG_20240102.jpg").exists())
            self.assertTrue((target / "2024" / "01" / "IMG_20240102-1.jpg").exists())

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
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

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
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

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
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            target_file = target / "2024" / "01" / "IMG_20240102.jpg"

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "show-conflict", str(target_file)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("ikke del av en navnekollisjon", stdout)

    def test_exiftool_metadata_gaps_lists_dates_bildebank_does_not_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            exiftool = target / "exiftool.exe"
            exiftool.write_text(
                """#!/usr/bin/env python3
import json
print(json.dumps([{"SourceFile": "x", "DateTimeOriginal": "2024:01:02 03:04:05"}]))
""",
                encoding="utf-8",
                newline="\n",
            )
            exiftool.chmod(0o755)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "exiftool-metadata-gaps"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("2024-01-02\tDateTimeOriginal", stdout)
            self.assertIn("bildebank=filename:2024-01-02", stdout)
            self.assertIn("IMG_20240102.jpg", stdout)
            self.assertIn("Oppsummering: exiftool_metadata_funnet=1", stdout)
            self.assertIn("exiftool 1/1:", stderr)

    def test_rejects_target_inside_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = source / "target"
            source.mkdir()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 1)

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
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)

            with patch("bilder.importer.os.walk", fake_walk):
                self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 2)

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

            with patch("bilder.importer.os.link", side_effect=OSError("hardlink unsupported")):
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
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)

            recovered = target / "2024" / "01" / "IMG_20240102.jpg"
            recovered.parent.mkdir(parents=True)
            recovered.write_bytes(b"already-copied")

            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)
            self.assertFalse((target / "2024" / "01" / "IMG_20240102-1.jpg").exists())

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0], 1)
            finally:
                conn.close()

    def test_import_removable_only_imports_that_source(self) -> None:
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
            self.assertEqual(run_cli(["--target", str(target), "add", str(normal)]), 0)
            self.assertEqual(
                run_cli(
                    [
                        "--target",
                        str(target),
                        "import-removable",
                        "--name",
                        "usb-test",
                        str(removable),
                    ]
                ),
                0,
            )

            self.assertFalse((target / "2024" / "01" / "NORMAL_20240102.jpg").exists())
            self.assertTrue((target / "2024" / "02" / "REM_20240203.jpg").exists())

    def test_import_removable_rejects_reused_imported_name_without_changing_path(self) -> None:
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
                run_cli(
                    [
                        "--target",
                        str(target),
                        "import-removable",
                        "--name",
                        "usb-test",
                        str(first),
                    ]
                ),
                0,
            )

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "import-removable",
                    "--name",
                    "usb-test",
                    str(second),
                ]
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

    def test_unimport_removable_requires_name_and_rejects_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            removable = root / "removable"
            removable.mkdir()
            (removable / "REM_20240203.jpg").write_bytes(b"removable")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(
                    [
                        "--target",
                        str(target),
                        "import-removable",
                        "--name",
                        "usb-test",
                        str(removable),
                    ]
                ),
                0,
            )

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "unimport", str(removable)]
            )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("må angis med --name", stderr)
            self.assertIn('bildebank unimport --name "usb-test"', stderr)
            self.assertTrue((target / "2024" / "02" / "REM_20240203.jpg").exists())

            with patch("builtins.input", return_value="ja, det vil jeg"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--name", "usb-test"]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Unimport gjennomført.", stdout)
            self.assertNotIn("Kilden er satt tilbake til pending.", stdout)
            self.assertFalse((target / "2024" / "02" / "REM_20240203.jpg").exists())
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)
            finally:
                conn.close()

    def test_unimport_removable_missing_source_file_explains_media_may_be_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            removable = root / "removable"
            removable.mkdir()
            source_file = removable / "REM_20240203.jpg"
            source_file.write_bytes(b"removable")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(
                    [
                        "--target",
                        str(target),
                        "import-removable",
                        "--name",
                        "usb-test",
                        str(removable),
                    ]
                ),
                0,
            )
            source_file.unlink()

            with patch("builtins.input", return_value="ja, det vil jeg"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "unimport", "--name", "usb-test"]
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Kildefil mangler", stderr)
            self.assertIn("Dette er et flyttbart medium", stderr)
            self.assertIn("Sjekk at riktig USB-disk", stderr)
            self.assertTrue((target / "2024" / "02" / "REM_20240203.jpg").exists())

    def test_remove_source_removable_requires_name_and_rejects_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            removable = root / "removable"
            removable.mkdir()
            (removable / "REM_20240203.jpg").write_bytes(b"removable")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(
                    [
                        "--target",
                        str(target),
                        "import-removable",
                        "--name",
                        "usb-test",
                        str(removable),
                    ]
                ),
                0,
            )

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "remove-source", str(removable)]
            )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("må angis med --name", stderr)
            self.assertIn('bildebank remove-source --name "usb-test"', stderr)

            code, stdout, stderr = capture_cli(["--target", str(target), "remove-source", "--name", "usb-test"])
            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn('bildebank unimport --name "usb-test"', stderr)

            with patch("builtins.input", return_value="ja, det vil jeg"):
                self.assertEqual(
                    run_cli(["--target", str(target), "unimport", "--name", "usb-test"]),
                    0,
                )

            code, stdout, stderr = capture_cli(["--target", str(target), "remove-source", "--name", "usb-test"])
            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Fant ikke flyttbart medium med navn", stderr)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)
            finally:
                conn.close()

    def test_import_removable_dry_run_does_not_register_or_copy(self) -> None:
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
                    "import-removable",
                    "--name",
                    "usb-test",
                    "--dry-run",
                    str(removable),
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("IMPORT\t2024-02-03\tfilename", stdout)
            self.assertIn(str(removable_file.resolve()), stdout)
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
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

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
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

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
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

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
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            old_target = target / "2008" / "02" / "video.avi"
            new_target = target / "2007" / "03" / "video.avi"
            self.assertTrue(old_target.exists())

            old_target.write_bytes(minimal_avi_with_idit_outside_info())

            code, stdout, stderr = capture_cli(["--target", str(target), "refresh-metadata"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("flyttet=1", stdout)
            self.assertFalse(old_target.exists())
            self.assertTrue(new_target.exists())

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                row = conn.execute(
                    "SELECT target_path, taken_date, date_source FROM files"
                ).fetchone()
                self.assertEqual(row[0], str(new_target.resolve()))
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
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

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
                    "insert into sources(kind, path, path_key) values('directory', ?, ?) returning id",
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
                        str(old_target),
                        str(old_target),
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
                        file_id, source_id, source_path, source_path_key, sha256, size_bytes, kind
                    ) values(?, ?, ?, ?, ?, ?, 'imported')
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
                self.assertEqual(row[0], str(repaired_target.resolve()))
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
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

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
            self.assertIn('img.loading = "lazy";', html)

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

    def test_make_browser_filters_by_media_and_date_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")
            (source / "video.mp4").write_bytes(minimal_mp4_with_creation_date(dt.date(2010, 7, 8)))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "make-browser",
                    "--media",
                    "video",
                    "--date-source",
                    "metadata",
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev HTML-browser", stdout)
            html = (target / "index.html").read_text(encoding="utf-8")
            self.assertIn('"path": "2010/07/video.mp4"', html)
            self.assertNotIn("IMG_20240102.jpg", html)

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

    def test_open_browser_opens_existing_index_without_rewriting_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)
            self.assertEqual(run_cli(["--target", str(target), "make-browser"]), 0)
            index = target / "index.html"
            index.write_text("custom browser\n", encoding="utf-8")

            with patch("bilder.cli.webbrowser.open", return_value=True) as browser_open:
                code, stdout, stderr = capture_cli(["--target", str(target), "open-browser"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Åpnet HTML-browser", stdout)
            self.assertEqual(index.read_text(encoding="utf-8"), "custom browser\n")
            browser_open.assert_called_once_with(index.resolve().as_uri())

            custom_browser = target / "annet.html"
            custom_browser.write_text("custom file\n", encoding="utf-8")
            with patch("bilder.cli.webbrowser.open", return_value=True) as browser_open:
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "open-browser", "--file", "annet.html"]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Åpnet HTML-browser", stdout)
            browser_open.assert_called_once_with(custom_browser.resolve().as_uri())

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
            self.assertEqual(run_cli(["--target", str(target), "add", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--quiet"]), 0)

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

            code, stdout, stderr = capture_cli(["--target", str(target), "add", str(source)])

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

    def test_migrate_check_reports_plan_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            create_legacy_database(target, source, include_duplicate=True)

            code, stdout, stderr = capture_cli(["--target", str(target), "migrate", "--check"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Nåværende schema_version: 1", stdout)
            self.assertIn("Ny schema_version: 3", stdout)
            self.assertIn("Vil opprette tabellen file_sources.", stdout)
            self.assertIn("  importerte filer: 1", stdout)
            self.assertIn("  duplikatfunn: 1", stdout)
            self.assertIn("  bygge om files uten gamle v1-kildekolonner", stdout)
            self.assertIn("  fjerne legacy-tabellen duplicate_findings", stdout)
            self.assertIn("Ingen endringer er gjort (--check).", stdout)
            self.assertFalse(list(target.glob(".bilder.sqlite3.backup-before-schema-3-*")))
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertFalse(
                    conn.execute(
                        "select 1 from sqlite_master where type = 'table' and name = 'file_sources'"
                    ).fetchone()
                )
            finally:
                conn.close()

    def test_migrate_backfills_file_sources_and_then_report_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            create_legacy_database(target, source, include_duplicate=True)

            code, stdout, stderr = capture_cli(["--target", str(target), "migrate"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Lager backup:", stdout)
            self.assertIn("Ferdig. Databasen er migrert.", stdout)
            self.assertEqual(len(list(target.glob(".bilder.sqlite3.backup-before-schema-3-*"))), 1)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("select value from meta where key = 'schema_version'").fetchone()[0],
                    "3",
                )
                self.assertEqual(conn.execute("select count(*) from file_sources").fetchone()[0], 2)
                kinds = [
                    row[0]
                    for row in conn.execute("select kind from file_sources order by id").fetchall()
                ]
                self.assertEqual(kinds, ["imported", "duplicate"])
                file_columns = {row[1] for row in conn.execute("pragma table_info(files)")}
                self.assertFalse({"source_id", "source_path", "source_path_key"} & file_columns)
                self.assertFalse(
                    conn.execute(
                        "select 1 from sqlite_master where type = 'table' and name = 'duplicate_findings'"
                    ).fetchone()
                )
                self.assertEqual(conn.execute("pragma foreign_key_list(errors)").fetchall(), [])
                self.assertEqual(conn.execute("select count(*) from command_log").fetchone()[0], 1)
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "report"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Importerte filer: 1", stdout)
            self.assertIn("Kildefilforekomster: 2", stdout)
            self.assertIn("Duplikatkilder: 1", stdout)

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
            self.assertEqual(len(list(target.glob(".bilder.sqlite3.backup-before-schema-3-*"))), 1)
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


if __name__ == "__main__":
    unittest.main()
