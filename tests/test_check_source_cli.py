from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.collection_paths import PathComponentIssue
from bildebank.db import DB_FILENAME
from tests.cli_helpers import capture_cli, run_cli


class CheckSourceCliTests(unittest.TestCase):
    def create_imported_collection(self, root: Path) -> tuple[Path, Path]:
        target = root / "target"
        source = root / "source"
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
        return target, source

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

            with patch("bildebank.cli_check_source.open_check_source_missing_report"):
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

    def test_check_source_uses_read_only_database_without_recovery_or_program_state_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, source = self.create_imported_collection(root)

            original_files_by_hash = db.files_by_hash
            query_only_values: list[int] = []

            def files_by_hash_read_only(conn, sha256):  # noqa: ANN001
                query_only_values.append(int(conn.execute("PRAGMA query_only").fetchone()[0]))
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("UPDATE files SET stored_filename = stored_filename")
                return original_files_by_hash(conn, sha256)

            with (
                patch("bildebank.cli.recover_pending_file_moves") as recover,
                patch("bildebank.cli.record_target_best_effort") as record_target,
                patch(
                    "bildebank.cli_check_source.db.files_by_hash",
                    side_effect=files_by_hash_read_only,
                ),
            ):
                code, _, stderr = capture_cli(
                    [
                        "--target",
                        str(target),
                        "check-source",
                        "--quiet",
                        str(source),
                    ]
                )

            self.assertEqual(code, 0, stderr)
            self.assertEqual(query_only_values, [1])
            recover.assert_not_called()
            record_target.assert_not_called()

    def test_check_source_rejects_database_integrity_error_before_hashing_targets(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, source = self.create_imported_collection(root)

            with (
                patch(
                    "bildebank.cli_check_source.db.database_integrity_errors",
                    return_value=["simulert integritetsfeil"],
                ),
                patch(
                    "bildebank.cli_check_source.hash_stable_collection_file"
                ) as hash_target,
            ):
                code, stdout, stderr = capture_cli(
                    [
                        "--target",
                        str(target),
                        "check-source",
                        "--quiet",
                        str(source),
                    ]
                )

            self.assertEqual(code, 2, stderr)
            self.assertIn("SQLite integrity_check fant 1 feil", stdout)
            self.assertIn("Kildemappen er derfor ikke trygg å slette.", stdout)
            self.assertNotIn("Remove-Item", stdout)
            hash_target.assert_not_called()

    def test_check_source_rejects_foreign_key_error_before_source_verdict(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, source = self.create_imported_collection(root)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute("UPDATE file_sources SET source_id = 999999")
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "check-source",
                    "--quiet",
                    str(source),
                ]
            )

            self.assertEqual(code, 2, stderr)
            self.assertIn("SQLite foreign_key_check fant 1", stdout)
            self.assertIn("Kildemappen er derfor ikke trygg å slette.", stdout)

    def test_check_source_rejects_old_schema_without_migrating_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, source = self.create_imported_collection(root)

            database_path = target / DB_FILENAME
            conn = sqlite3.connect(database_path)
            try:
                conn.execute(
                    "UPDATE meta SET value = ? WHERE key = 'schema_version'",
                    (str(db.SCHEMA_VERSION - 1),),
                )
                conn.commit()
            finally:
                conn.close()
            content_before = database_path.read_bytes()

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "check-source",
                    "--quiet",
                    str(source),
                ]
            )

            self.assertEqual(code, 2, stderr)
            self.assertIn("gjeldende databaseschema kunne ikke bekreftes", stdout)
            self.assertEqual(database_path.read_bytes(), content_before)

    def test_check_source_rejects_unsafe_database_path_before_target_hash(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, source = self.create_imported_collection(root)
            (root / "outside.jpg").write_bytes(b"image")

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute(
                    """
                    UPDATE files
                    SET target_path = '../outside.jpg',
                        target_path_key = '../outside.jpg'
                    """
                )
                conn.commit()
            finally:
                conn.close()

            with patch(
                "bildebank.cli_check_source.hash_stable_collection_file"
            ) as hash_target:
                code, stdout, stderr = capture_cli(
                    [
                        "--target",
                        str(target),
                        "check-source",
                        "--quiet",
                        str(source),
                    ]
                )

            self.assertEqual(code, 2, stderr)
            self.assertIn("databaseført(e) stifeil", stdout)
            self.assertIn("kan ikke inneholde ..", stdout)
            hash_target.assert_not_called()

    def test_check_source_rejects_unsafe_database_path_component(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, source = self.create_imported_collection(root)

            issue = PathComponentIssue(
                path=target / "2024",
                reason="stikomponenten er en symlink eller et Windows reparse point",
            )
            with patch(
                "bildebank.cli_check_source.inspect_existing_collection_path_components",
                return_value=issue,
            ):
                code, stdout, stderr = capture_cli(
                    [
                        "--target",
                        str(target),
                        "check-source",
                        "--quiet",
                        str(source),
                    ]
                )

            self.assertEqual(code, 2, stderr)
            self.assertIn("usikker stikomponent", stdout)
            self.assertIn("Windows reparse point", stdout)

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

            with patch("bildebank.cli_check_source.open_check_source_missing_report"):
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

            with patch("bildebank.cli_check_source.open_check_source_missing_report", fake_open):
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

            with patch("bildebank.cli_check_source.open_check_source_missing_report", fake_open):
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

            with patch("bildebank.cli_check_source.open_check_source_missing_report") as open_report:
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

            with patch("bildebank.cli_check_source.open_check_source_missing_report", fake_open):
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

            with patch("bildebank.cli_check_source.open_check_source_missing_report") as open_report:
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

            with patch("bildebank.cli_check_source.open_check_source_missing_report"):
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

            with patch("bildebank.cli_check_source.open_check_source_missing_report") as open_report:
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "check-source", "--quiet", str(source)]
                )

            self.assertEqual(code, 2, stderr)
            self.assertIn("scannet=1, dekket=0, mangler=0", stdout)
            self.assertIn("målfeil=1", stdout)
            self.assertIn("matchende fil i bildesamlingen mangler eller har endret innhold", stdout)
            open_report.assert_called_once()
