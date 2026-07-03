from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bildebank.db import DB_FILENAME
from bildebank.target_lock import LOCK_FILENAME
from tests.cli_helpers import capture_cli, run_cli


class BackupCliTests(unittest.TestCase):
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
            (target / ".bildebank.log").write_text("lokal logg\n", encoding="utf-8")

            with patch("bildebank.backup.select_backup_engine", return_value=None):
                code, stdout, stderr = capture_cli(["--target", str(target), "backup", str(backup_parent)])

            self.assertEqual(code, 0, stderr)
            self.assertIn("ADVARSEL: robocopy/rsync mangler", stdout)
            self.assertIn("files_copied=", stdout)
            backup_dir = backup_parent / target.name
            self.assertIn(str(backup_dir), stdout)
            self.assertEqual((backup_dir / "2024" / "01" / "IMG_20240102.jpg").read_bytes(), b"image")
            self.assertEqual((backup_dir / "deleted" / "2024" / "01" / "IMG_20240103.jpg").read_bytes(), b"removed")
            self.assertFalse((backup_dir / ".bildebank.log").exists())
            self.assertFalse((backup_dir / LOCK_FILENAME).exists())
            metadata = json.loads((backup_dir / ".bildebank-backup.json").read_text(encoding="utf-8"))
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                collection_id = conn.execute("SELECT value FROM meta WHERE key = 'collection_id'").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(metadata["backup_of"], collection_id)
            self.assertEqual(metadata["source_name"], target.name)

            (backup_dir / ".bildebank.log").write_text("gammel logg\n", encoding="utf-8")
            (backup_dir / LOCK_FILENAME).write_text("command=backup\n", encoding="utf-8")
            with patch("bildebank.backup.select_backup_engine", return_value=None):
                code, _stdout, stderr = capture_cli(["--target", str(target), "backup", str(backup_parent)])

            self.assertEqual(code, 0, stderr)
            self.assertFalse((backup_dir / ".bildebank.log").exists())
            self.assertFalse((backup_dir / LOCK_FILENAME).exists())

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
            self.assertIn(LOCK_FILENAME, command)
            self.assertIn(".bildebank.log", command)
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
            self.assertIn(f"bildebank backup --adopt --dry-run {backup_parent}", stderr)
            self.assertIn(f"bildebank backup --adopt {backup_parent}", stderr)
            self.assertEqual((backup_dir / "unrelated.txt").read_text(encoding="utf-8"), "do not touch\n")

    def test_backup_rejects_existing_backup_without_backup_of_with_specific_message(self) -> None:
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
            metadata_path = backup_dir / ".bildebank-backup.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            del metadata["backup_of"]
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            extra = backup_dir / "extra.txt"
            extra.write_text("do not delete\n", encoding="utf-8")

            code, stdout, stderr = capture_cli(["--target", str(target), "backup", str(backup_parent)])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("backupmetadata mangler feltet backup_of", stderr)
            self.assertIn(str(metadata_path), stderr)
            self.assertIn(f"bildebank backup --adopt --dry-run {backup_parent}", stderr)
            self.assertIn(f"bildebank backup --adopt {backup_parent}", stderr)
            self.assertNotIn("backup for en annen bildesamling", stderr)
            self.assertEqual(extra.read_text(encoding="utf-8"), "do not delete\n")

    def test_backup_adopt_dry_run_reports_file_comparison_without_writing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            backup_parent = root / "backup-root"
            backup_dir = backup_parent / target.name
            source.mkdir()
            backup_dir.mkdir(parents=True)
            (source / "IMG_20240102.jpg").write_bytes(b"image")
            (source / "IMG_20240103.jpg").write_bytes(b"second")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", "source", "--quiet", str(source)]),
                0,
            )
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute(
                    """
                    INSERT INTO files(
                        target_path, target_path_key, original_filename, stored_filename,
                        sha256, size_bytes, date_source, deleted_at, deleted_original_target_path
                    )
                    VALUES(
                        'deleted/2024/01/IMG_20240104.jpg',
                        'deleted/2024/01/img_20240104.jpg',
                        'IMG_20240104.jpg',
                        'IMG_20240104.jpg',
                        'abc',
                        7,
                        'filename',
                        CURRENT_TIMESTAMP,
                        '2024/01/IMG_20240104.jpg'
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

            (backup_dir / "2024" / "01").mkdir(parents=True)
            (backup_dir / "2024" / "01" / "IMG_20240102.jpg").write_bytes(b"image")
            (backup_dir / "2024" / "01" / "IMG_20240103.jpg").write_bytes(b"wrong-size")
            (backup_dir / "extra_20240105.jpg").write_bytes(b"extra")

            with patch("builtins.input", side_effect=AssertionError("dry-run should not ask")):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "backup", "--adopt", "--dry-run", str(backup_parent)]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Adopt dry run", stdout)
            self.assertIn("Mangler .bildebank-backup.json", stdout)
            self.assertIn("database_files=3", stdout)
            self.assertIn("matched=1 (33.3%)", stdout)
            self.assertIn("missing=1", stdout)
            self.assertIn("wrong_size_or_type=1", stdout)
            self.assertIn("extra_media_files=1", stdout)
            self.assertIn("Dry-run: ingen endringer er gjort.", stdout)
            self.assertFalse((backup_dir / ".bildebank-backup.json").exists())

    def test_backup_adopt_registers_existing_directory_after_exact_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            backup_parent = root / "backup-root"
            backup_dir = backup_parent / target.name
            backup_dir.mkdir(parents=True)

            self.assertEqual(run_cli(["create", str(target)]), 0)

            with patch("builtins.input", return_value="registrer backup"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "backup", "--adopt", str(backup_parent)]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Ready to register backup.", stdout)
            self.assertIn("Backupen er registrert", stdout)
            metadata = json.loads((backup_dir / ".bildebank-backup.json").read_text(encoding="utf-8"))
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                collection_id = conn.execute("SELECT value FROM meta WHERE key = 'collection_id'").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(metadata["backup_of"], collection_id)
            self.assertEqual(metadata["source_name"], target.name)
            self.assertEqual(metadata["status"], "adopted")
            self.assertEqual(metadata["created_by"], "bildebank")

    def test_backup_adopt_repairs_metadata_without_backup_of_and_preserves_extra_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            backup_parent = root / "backup-root"
            backup_dir = backup_parent / target.name
            backup_dir.mkdir(parents=True)
            metadata_path = backup_dir / ".bildebank-backup.json"
            metadata_path.write_text(
                json.dumps({"source_name": "gammelt-navn", "note": "behold"}),
                encoding="utf-8",
            )

            self.assertEqual(run_cli(["create", str(target)]), 0)

            with patch("builtins.input", return_value="registrer backup"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "backup", "--adopt", str(backup_parent)]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Metadata finnes, men backup_of mangler", stdout)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["source_name"], target.name)
            self.assertEqual(metadata["note"], "behold")
            self.assertIn("backup_of", metadata)

    def test_backup_adopt_aborts_without_exact_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            backup_parent = root / "backup-root"
            backup_dir = backup_parent / target.name
            backup_dir.mkdir(parents=True)

            self.assertEqual(run_cli(["create", str(target)]), 0)

            with patch("builtins.input", return_value="ja"):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "backup", "--adopt", str(backup_parent)]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Avbrutt. Ingen endringer er gjort.", stdout)
            self.assertFalse((backup_dir / ".bildebank-backup.json").exists())

    def test_backup_adopt_rejects_existing_matching_or_mismatching_backup_of(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            backup_parent = root / "backup-root"
            backup_dir = backup_parent / target.name
            backup_dir.mkdir(parents=True)

            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                collection_id = conn.execute("SELECT value FROM meta WHERE key = 'collection_id'").fetchone()[0]
            finally:
                conn.close()
            metadata_path = backup_dir / ".bildebank-backup.json"
            metadata_path.write_text(json.dumps({"backup_of": collection_id}), encoding="utf-8")

            code, stdout, stderr = capture_cli(["--target", str(target), "backup", "--adopt", str(backup_parent)])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("allerede registrert", stderr)

            metadata_path.write_text(json.dumps({"backup_of": str(uuid.uuid4())}), encoding="utf-8")

            code, stdout, stderr = capture_cli(["--target", str(target), "backup", "--adopt", str(backup_parent)])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("backup for en annen bildesamling", stderr)

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
            self.assertIn(LOCK_FILENAME, command)
            self.assertIn(".bildebank.log", command)
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
            self.assertIn(LOCK_FILENAME, command)
            self.assertIn(".bildebank.log", command)

