from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.db import DB_FILENAME
from bildebank.program_state import PROGRAM_DB_FILENAME, ensure_schema, known_targets, record_target
from tests.cli_helpers import capture_cli, run_cli


class WhereProgramCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.program_root_tempdir = tempfile.TemporaryDirectory()
        self.program_root = Path(self.program_root_tempdir.name)
        self.program_root_patcher = patch("bildebank.cli.program_repo_root", return_value=self.program_root)
        self.program_root_patcher.start()

    def tearDown(self) -> None:
        self.program_root_patcher.stop()
        self.program_root_tempdir.cleanup()

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

    def test_where_is_works_without_target(self) -> None:
        code, stdout, stderr = capture_cli(["where-is"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("Bildebank-program:", stdout)
        self.assertIn("Ingen registrert ennå.", stdout)

