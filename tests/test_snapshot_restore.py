from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.snapshot import REPOSITORY_LOCK_FILENAME, snapshot_object_path
from bildebank.snapshot_create import create_snapshot
from bildebank.snapshot_repository import SnapshotStorageError
from bildebank.snapshot_restore import (
    RECOVERY_REPORT_FILENAME,
    plan_full_restore,
    plan_single_file_restore,
    restore_full_snapshot,
)
from tests.cli_helpers import capture_cli


class SnapshotRestorePlanTests(unittest.TestCase):
    def test_full_restore_plan_is_read_only_and_includes_files_and_main_database(self) -> None:
        with normal_snapshot() as (root, target, repository, snapshot_id):
            restore_target = root / "restored"
            repository_before = tree_file_bytes(repository)

            plan = plan_full_restore(repository, snapshot_id, restore_target)

            self.assertEqual(plan.target_state, "missing")
            self.assertEqual(
                {output.relative_path for output in plan.collection_outputs},
                {".bilder.sqlite3", "notater.txt"},
            )
            self.assertIsNone(plan.recovery_target)
            self.assertFalse(plan.incomplete)
            self.assertTrue(plan.original_collection_exists)
            self.assertEqual(plan.original_collection, target)
            self.assertFalse(restore_target.exists())
            self.assertEqual(tree_file_bytes(repository), repository_before)
            self.assertFalse((repository / REPOSITORY_LOCK_FILENAME).exists())

    def test_full_restore_accepts_empty_target_but_rejects_nonempty_and_overlapping_targets(self) -> None:
        with normal_snapshot() as (root, target, repository, snapshot_id):
            empty = root / "empty"
            empty.mkdir()
            self.assertEqual(
                plan_full_restore(repository, snapshot_id, empty).target_state,
                "empty",
            )
            (empty / "bevar.txt").write_text("ikke rør\n", encoding="utf-8")

            with self.assertRaisesRegex(SnapshotStorageError, "ikke tom"):
                plan_full_restore(repository, snapshot_id, empty)
            with self.assertRaisesRegex(SnapshotStorageError, "repositoryet"):
                plan_full_restore(repository, snapshot_id, repository / "restore")
            with self.assertRaisesRegex(SnapshotStorageError, "opprinnelige bildesamlingen"):
                plan_full_restore(repository, snapshot_id, target / "restore")

            self.assertEqual((empty / "bevar.txt").read_text(encoding="utf-8"), "ikke rør\n")

    def test_degraded_restore_uses_expected_variant_and_separate_observed_recovery(self) -> None:
        with degraded_snapshot() as (root, _target, repository, snapshot_id, expected, observed):
            restore_target = root / "restored"

            plan = plan_full_restore(repository, snapshot_id, restore_target)

            media = next(
                output for output in plan.collection_outputs if output.relative_path == "2026/07/familie.jpg"
            )
            recovery = next(
                output for output in plan.recovery_outputs if "familie.observed-" in output.relative_path
            )
            self.assertEqual(media.object.sha256, hashlib.sha256(expected).hexdigest())
            self.assertEqual(media.variant, "expected")
            self.assertEqual(recovery.object.sha256, hashlib.sha256(observed).hexdigest())
            self.assertEqual(recovery.variant, "observed")
            self.assertIsNotNone(plan.recovery_target)
            self.assertFalse(plan.incomplete)

    def test_degraded_restore_is_incomplete_when_expected_variant_is_unavailable(self) -> None:
        with degraded_snapshot() as (root, _target, repository, snapshot_id, expected, _observed):
            expected_sha256 = hashlib.sha256(expected).hexdigest()
            snapshot_object_path(repository, expected_sha256, len(expected)).unlink()

            plan = plan_full_restore(repository, snapshot_id, root / "restored")

            self.assertTrue(plan.incomplete)
            self.assertEqual(len(plan.missing_expected_entries), 1)
            self.assertNotIn(
                "2026/07/familie.jpg",
                {output.relative_path for output in plan.collection_outputs},
            )
            self.assertEqual(len(plan.recovery_outputs), 1)

    def test_restore_plan_rejects_recovery_snapshot_and_existing_recovery_target(self) -> None:
        with normal_snapshot() as (root, target, repository, _snapshot_id):
            (target / db.DB_FILENAME).write_bytes(b"skadet database")
            recovery_result = create_snapshot(target, repository)
            self.assertEqual(recovery_result.status, "recovery")

            with self.assertRaisesRegex(SnapshotStorageError, "kan ikke gjenopprettes"):
                plan_full_restore(repository, recovery_result.published.snapshot_id, root / "restored")

        with degraded_snapshot() as (root, _target, repository, snapshot_id, _expected, _observed):
            first_plan = plan_full_restore(repository, snapshot_id, root / "restored")
            assert first_plan.recovery_target is not None
            first_plan.recovery_target.mkdir()

            with self.assertRaisesRegex(SnapshotStorageError, "finnes allerede"):
                plan_full_restore(repository, snapshot_id, root / "restored")

    def test_restore_plan_reports_existing_staging_without_removing_it(self) -> None:
        with normal_snapshot() as (root, _target, repository, snapshot_id):
            staging = root / ".bildebank-restore-restored-tidligere"
            staging.mkdir()
            marker = staging / "bevar.bin"
            marker.write_bytes(b"bevar")

            with self.assertRaisesRegex(SnapshotStorageError, "ufullstendig restore"):
                plan_full_restore(repository, snapshot_id, root / "restored")

            self.assertEqual(marker.read_bytes(), b"bevar")

    def test_single_file_plan_requires_variant_when_expected_and_observed_exist(self) -> None:
        with degraded_snapshot() as (root, _target, repository, snapshot_id, expected, observed):
            export = root / "export"
            with self.assertRaisesRegex(SnapshotStorageError, "Velg --variant"):
                plan_single_file_restore(
                    repository,
                    snapshot_id,
                    export,
                    path="2026/07/familie.jpg",
                )

            expected_plan = plan_single_file_restore(
                repository,
                snapshot_id,
                export,
                path="2026/07/familie.jpg",
                variant="expected",
            )
            observed_plan = plan_single_file_restore(
                repository,
                snapshot_id,
                export,
                path="2026/07/familie.jpg",
                variant="observed",
            )

            self.assertEqual(expected_plan.output.object.sha256, hashlib.sha256(expected).hexdigest())
            self.assertEqual(expected_plan.output.relative_path, "2026/07/familie.jpg")
            self.assertEqual(observed_plan.output.object.sha256, hashlib.sha256(observed).hexdigest())
            self.assertIn(".observed-", observed_plan.output.relative_path)
            self.assertFalse(export.exists())

    def test_single_file_plan_rejects_existing_output_without_overwriting(self) -> None:
        with normal_snapshot() as (root, _target, repository, snapshot_id):
            output = root / "export" / "notater.txt"
            output.parent.mkdir()
            output.write_text("bevar\n", encoding="utf-8")

            with self.assertRaisesRegex(SnapshotStorageError, "blir ikke overskrevet"):
                plan_single_file_restore(
                    repository,
                    snapshot_id,
                    output.parent,
                    path="notater.txt",
                )

            self.assertEqual(output.read_text(encoding="utf-8"), "bevar\n")

    def test_full_restore_publishes_verified_collection_and_preserves_repository(self) -> None:
        with normal_snapshot() as (root, target, repository, snapshot_id):
            restored = root / "restored"
            repository_before = tree_file_bytes(repository)
            original_collection_id = read_collection_id(target)
            original_mtime_ns = (target / "notater.txt").stat().st_mtime_ns

            result = restore_full_snapshot(repository, snapshot_id, restored)

            self.assertEqual(result.exit_code, 0)
            self.assertEqual((restored / "notater.txt").read_text(encoding="utf-8"), "familienotat\n")
            self.assertEqual((restored / "notater.txt").stat().st_mtime_ns, original_mtime_ns)
            self.assertEqual(read_collection_id(restored), original_collection_id)
            connection = sqlite3.connect(restored / db.DB_FILENAME)
            try:
                db.validate_database_health(connection)
            finally:
                connection.close()
            self.assertEqual(tree_file_bytes(repository), repository_before)
            self.assertEqual(list(root.glob(".bildebank-restore-restored-*")), [])
            self.assertFalse((repository / REPOSITORY_LOCK_FILENAME).exists())

    def test_full_restore_replaces_only_still_empty_selected_target(self) -> None:
        with normal_snapshot() as (root, _target, repository, snapshot_id):
            restored = root / "restored"
            restored.mkdir()

            result = restore_full_snapshot(repository, snapshot_id, restored)

            self.assertEqual(result.published_target, restored)
            self.assertTrue((restored / db.DB_FILENAME).is_file())

    def test_degraded_restore_publishes_expected_file_and_observed_recovery_report(self) -> None:
        with degraded_snapshot() as (root, _target, repository, snapshot_id, expected, observed):
            restored = root / "restored"

            result = restore_full_snapshot(repository, snapshot_id, restored)

            self.assertEqual(result.exit_code, 0)
            self.assertEqual((restored / "2026/07/familie.jpg").read_bytes(), expected)
            assert result.published_recovery is not None
            observed_files = list(result.published_recovery.rglob("familie.observed-*.jpg"))
            self.assertEqual(len(observed_files), 1)
            self.assertEqual(observed_files[0].read_bytes(), observed)
            report = result.published_recovery / RECOVERY_REPORT_FILENAME
            self.assertIn("2026/07/familie.jpg", report.read_text(encoding="utf-8"))

    def test_incomplete_restore_publishes_without_expected_file_and_returns_three(self) -> None:
        with degraded_snapshot() as (root, _target, repository, snapshot_id, expected, observed):
            snapshot_object_path(
                repository,
                hashlib.sha256(expected).hexdigest(),
                len(expected),
            ).unlink()
            restored = root / "restored"

            result = restore_full_snapshot(repository, snapshot_id, restored)

            self.assertEqual(result.exit_code, 3)
            self.assertFalse((restored / "2026/07/familie.jpg").exists())
            assert result.published_recovery is not None
            observed_file = next(result.published_recovery.rglob("familie.observed-*.jpg"))
            self.assertEqual(observed_file.read_bytes(), observed)

    def test_copy_failure_keeps_staging_and_does_not_publish_collection(self) -> None:
        with normal_snapshot() as (root, _target, repository, snapshot_id):
            restored = root / "restored"
            from bildebank.snapshot_restore import copy_verified_restore_object

            calls = 0

            def fail_after_first_copy(repository_path, output, destination) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulert skrivefeil")
                copy_verified_restore_object(repository_path, output, destination)

            with (
                patch(
                    "bildebank.snapshot_restore.copy_verified_restore_object",
                    side_effect=fail_after_first_copy,
                ),
                self.assertRaisesRegex(SnapshotStorageError, "Staging er bevart"),
            ):
                restore_full_snapshot(repository, snapshot_id, restored)

            staging = next(root.glob(".bildebank-restore-restored-*"))
            self.assertTrue((staging / "run.json").is_file())
            self.assertTrue(any((staging / "collection").rglob("*")))
            self.assertFalse(restored.exists())
            self.assertFalse((repository / REPOSITORY_LOCK_FILENAME).exists())
            with self.assertRaisesRegex(SnapshotStorageError, "ufullstendig restore"):
                plan_full_restore(repository, snapshot_id, restored)

    def test_same_size_corrupt_object_is_rejected_during_copy(self) -> None:
        with normal_snapshot() as (root, _target, repository, snapshot_id):
            restored = root / "restored"
            plan = plan_full_restore(repository, snapshot_id, restored)
            notes = next(output for output in plan.collection_outputs if output.relative_path == "notater.txt")
            object_path = snapshot_object_path(
                repository,
                notes.object.sha256,
                notes.object.size_bytes,
            )
            object_path.write_bytes(b"x" * notes.object.size_bytes)

            with self.assertRaisesRegex(SnapshotStorageError, "SHA-256-kontroll under kopiering"):
                restore_full_snapshot(repository, snapshot_id, restored)

            self.assertFalse(restored.exists())
            self.assertEqual(len(list(root.glob(".bildebank-restore-restored-*"))), 1)

    def test_target_filled_before_publish_is_preserved_and_staging_remains(self) -> None:
        with normal_snapshot() as (root, _target, repository, snapshot_id):
            restored = root / "restored"
            from bildebank.snapshot_restore import validate_staged_restore

            def fill_target_after_validation(*args, **kwargs) -> None:
                validate_staged_restore(*args, **kwargs)
                restored.mkdir()
                (restored / "annen-prosess.txt").write_text("bevar\n", encoding="utf-8")

            with (
                patch(
                    "bildebank.snapshot_restore.validate_staged_restore",
                    side_effect=fill_target_after_validation,
                ),
                self.assertRaisesRegex(SnapshotStorageError, "blir ikke overskrevet"),
            ):
                restore_full_snapshot(repository, snapshot_id, restored)

            self.assertEqual(
                (restored / "annen-prosess.txt").read_text(encoding="utf-8"),
                "bevar\n",
            )
            self.assertEqual(len(list(root.glob(".bildebank-restore-restored-*"))), 1)

    def test_collection_publish_failure_keeps_staging_after_recovery_was_published(self) -> None:
        with degraded_snapshot() as (root, _target, repository, snapshot_id, _expected, _observed):
            restored = root / "restored"
            real_rename = os.rename

            def fail_collection_rename(source, destination) -> None:
                if Path(source).name == "collection":
                    raise OSError("simulert rename-feil")
                real_rename(source, destination)

            with (
                patch("bildebank.snapshot_restore.os.rename", side_effect=fail_collection_rename),
                self.assertRaisesRegex(SnapshotStorageError, "Recovery-mappen er allerede publisert"),
            ):
                restore_full_snapshot(repository, snapshot_id, restored)

            self.assertFalse(restored.exists())
            recovery = next(root.glob("restored-recovery-*"))
            self.assertTrue((recovery / RECOVERY_REPORT_FILENAME).is_file())
            staging = next(root.glob(".bildebank-restore-restored-*"))
            self.assertTrue((staging / "collection").is_dir())

    def test_cli_full_and_single_file_dry_run_do_not_require_active_collection(self) -> None:
        with normal_snapshot() as (root, target, repository, snapshot_id):
            target.rename(root / "collection-moved")
            restore_target = root / "restored"
            export_target = root / "export"
            repository_before = tree_file_bytes(repository)

            full_code, full_stdout, full_stderr = capture_cli(
                [
                    "snapshot",
                    "restore",
                    str(repository),
                    snapshot_id,
                    str(restore_target),
                    "--dry-run",
                ]
            )
            file_code, file_stdout, file_stderr = capture_cli(
                [
                    "snapshot",
                    "restore-file",
                    str(repository),
                    snapshot_id,
                    str(export_target),
                    "--path",
                    "notater.txt",
                    "--dry-run",
                ]
            )

            self.assertEqual(full_code, 0, full_stderr)
            self.assertIn("Hel restore dry-run", full_stdout)
            self.assertIn("Ordinære utdatafiler: 2", full_stdout)
            self.assertEqual(file_code, 0, file_stderr)
            self.assertIn("Enkeltfil-restore dry-run", file_stdout)
            self.assertIn("notater.txt", file_stdout)
            self.assertFalse(restore_target.exists())
            self.assertFalse(export_target.exists())
            self.assertEqual(tree_file_bytes(repository), repository_before)

    def test_cli_requires_exact_confirmation_before_real_restore(self) -> None:
        with normal_snapshot() as (root, _target, repository, snapshot_id):
            destination = root / "restored"

            with patch("builtins.input", return_value="nei"):
                code, stdout, stderr = capture_cli(
                    ["snapshot", "restore", str(repository), snapshot_id, str(destination)]
                )

            self.assertEqual(code, 1)
            self.assertIn("Plan for hel restore", stdout)
            self.assertIn("Restore avbrutt", stderr)
            self.assertFalse(destination.exists())

    def test_cli_yes_publishes_full_restore(self) -> None:
        with normal_snapshot() as (root, _target, repository, snapshot_id):
            destination = root / "restored"

            code, stdout, stderr = capture_cli(
                [
                    "snapshot",
                    "restore",
                    str(repository),
                    snapshot_id,
                    str(destination),
                    "--yes",
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Hel restore publisert", stdout)
            self.assertTrue((destination / db.DB_FILENAME).is_file())

    def test_cli_exact_confirmation_publishes_full_restore(self) -> None:
        with normal_snapshot() as (root, _target, repository, snapshot_id):
            destination = root / "restored"

            with patch("builtins.input", return_value=f"GJENOPPRETT {snapshot_id}"):
                code, stdout, stderr = capture_cli(
                    ["snapshot", "restore", str(repository), snapshot_id, str(destination)]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Hel restore publisert", stdout)
            self.assertTrue((destination / db.DB_FILENAME).is_file())


class normal_snapshot:
    def __init__(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()

    def __enter__(self) -> tuple[Path, Path, Path, str]:
        root = Path(self._tempdir.name)
        target = root / "collection"
        repository = root / "repository"
        db.init_database(target)
        (target / "notater.txt").write_text("familienotat\n", encoding="utf-8")
        result = create_snapshot(target, repository, note="Restore-test")
        return root, target, repository, result.published.snapshot_id

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self._tempdir.cleanup()


class degraded_snapshot:
    def __init__(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()

    def __enter__(self) -> tuple[Path, Path, Path, str, bytes, bytes]:
        root = Path(self._tempdir.name)
        target = root / "collection"
        repository = root / "repository"
        db.init_database(target)
        expected = b"original"
        observed = b"changed!"
        relative_path = "2026/07/familie.jpg"
        media = target / relative_path
        media.parent.mkdir(parents=True)
        media.write_bytes(expected)
        insert_database_file(target, relative_path, hashlib.sha256(expected).hexdigest(), len(expected))
        create_snapshot(target, repository)
        media.write_bytes(observed)
        result = create_snapshot(target, repository)
        assert result.status == "degraded"
        return root, target, repository, result.published.snapshot_id, expected, observed

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self._tempdir.cleanup()


def insert_database_file(target: Path, relative_path: str, sha256: str, size_bytes: int) -> None:
    connection = sqlite3.connect(target / db.DB_FILENAME)
    try:
        connection.execute(
            """
            INSERT INTO files(
                target_path, target_path_key, original_filename, stored_filename,
                sha256, size_bytes, taken_date, date_source, name_conflict
            )
            VALUES(?, ?, ?, ?, ?, ?, '2026-07-15', 'filename', 0)
            """,
            (
                relative_path,
                relative_path.casefold(),
                Path(relative_path).name,
                Path(relative_path).name,
                sha256,
                size_bytes,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def read_collection_id(target: Path) -> str:
    connection = sqlite3.connect(target / db.DB_FILENAME)
    connection.row_factory = sqlite3.Row
    try:
        return db.validate_collection_id(connection)
    finally:
        connection.close()


def tree_file_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
