from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.config import FaceRecognitionConfig
from bildebank.snapshot import (
    REPOSITORY_LOCK_FILENAME,
    portable_path_key,
    snapshot_object_path,
)
from bildebank.snapshot_create import create_snapshot
from bildebank.snapshot_repository import SnapshotStorageError
from bildebank.snapshot_restore import (
    RECOVERY_REPORT_FILENAME,
    list_snapshot_problems,
    plan_full_restore,
    plan_single_file_restore,
    restore_full_snapshot,
    restore_single_file,
)
from tests.cli_helpers import capture_cli


class SnapshotRestorePlanTests(unittest.TestCase):
    def test_snapshot_problems_lists_degraded_entry_variants_and_is_read_only(self) -> None:
        with degraded_snapshot() as (root, _target, repository, snapshot_id, _expected, _observed):
            repository_before = tree_file_bytes(repository)

            result = list_snapshot_problems(repository, snapshot_id)

            self.assertEqual(len(result.snapshots), 1)
            self.assertEqual(len(result.file_problems), 1)
            problem = result.file_problems[0]
            self.assertEqual(problem.snapshot.snapshot_id, snapshot_id)
            self.assertEqual(problem.entry.path, "2026/07/familie.jpg")
            self.assertEqual(problem.recorded_variants, ("expected", "observed"))
            self.assertEqual(tree_file_bytes(repository), repository_before)
            self.assertFalse((repository / REPOSITORY_LOCK_FILENAME).exists())

    def test_snapshot_problems_lists_recovery_only_entry_and_database_problem(self) -> None:
        with recovery_only_snapshot() as (_root, repository, snapshot_id, entry_id, _raw_bytes):
            result = list_snapshot_problems(repository)

            self.assertEqual(len(result.snapshots), 1)
            self.assertTrue(
                any(problem.entry.entry_id == entry_id for problem in result.file_problems)
            )
            self.assertTrue(
                any(problem.role == "openclip" for problem in result.database_problems)
            )

    def test_cli_snapshot_problems_shows_entry_id_for_restore(self) -> None:
        with degraded_snapshot() as (_root, _target, repository, snapshot_id, _expected, _observed):
            code, stdout, stderr = capture_cli(
                ["snapshot", "problems", str(repository), snapshot_id]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Problemer i versjonerte snapshots", stdout)
            self.assertIn("Entry-ID: e-000000000001", stdout)
            self.assertIn("Registrerte varianter: expected, observed", stdout)

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

    def test_absolute_face_database_restore_warns_without_changing_old_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "collection"
            repository = root / "repository"
            face_dir = root / "external-faces"
            face_database = face_dir / "antelopev2.sqlite3"
            db.init_database(target)
            create_sqlite_database(face_database, schema_version=5)
            face_database_before = face_database.read_bytes()

            created = create_snapshot(
                target,
                repository,
                face_config=FaceRecognitionConfig(database_dir=face_dir),
            )
            warning_fragment = "absolutt database_dir"
            config_fragment = "face_recognition.database_dir"
            self.assertTrue(
                any(warning_fragment in warning for warning in created.build.warnings)
            )
            manifest = json.loads(
                (created.published.snapshot_dir / "manifest.json").read_bytes()
            )
            self.assertTrue(
                any(warning_fragment in warning for warning in manifest["warnings"])
            )

            dry_target = root / "dry-restored"
            plan = plan_full_restore(
                repository,
                created.published.snapshot_id,
                dry_target,
            )
            self.assertTrue(any(config_fragment in warning for warning in plan.warnings))

            dry_code, _dry_stdout, dry_stderr = capture_cli(
                [
                    "snapshot",
                    "restore",
                    str(repository),
                    created.published.snapshot_id,
                    str(dry_target),
                    "--dry-run",
                ]
            )
            restored = root / "restored"
            restore_code, _restore_stdout, restore_stderr = capture_cli(
                [
                    "snapshot",
                    "restore",
                    str(repository),
                    created.published.snapshot_id,
                    str(restored),
                    "--yes",
                ]
            )

            self.assertEqual(dry_code, 0, dry_stderr)
            self.assertIn(config_fragment, dry_stderr)
            self.assertEqual(restore_code, 0, restore_stderr)
            self.assertIn(config_fragment, restore_stderr)
            self.assertTrue(
                (restored / ".bildebank-faces" / "antelopev2.sqlite3").is_file()
            )
            self.assertEqual(face_database.read_bytes(), face_database_before)

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

    def test_database_file_with_recovery_only_path_makes_restore_incomplete(self) -> None:
        with database_file_recovery_only_snapshot() as (
            root,
            _target,
            repository,
            snapshot_id,
            entry_id,
            _media_bytes,
        ):
            plan = plan_full_restore(repository, snapshot_id, root / "restored")

            self.assertTrue(plan.incomplete)
            self.assertEqual(plan.missing_expected_entries, (entry_id,))
            self.assertNotIn(
                "2026/07/familie.jpg",
                {output.relative_path for output in plan.collection_outputs},
            )
            self.assertEqual(
                [output.entry_id for output in plan.recovery_outputs],
                [entry_id],
            )

    def test_raw_database_recovery_only_does_not_make_restore_incomplete(self) -> None:
        with recovery_only_snapshot() as (root, repository, snapshot_id, entry_id, _raw_bytes):
            plan = plan_full_restore(repository, snapshot_id, root / "restored")

            self.assertFalse(plan.incomplete)
            self.assertEqual(plan.missing_expected_entries, ())
            self.assertIn(
                entry_id,
                {output.entry_id for output in plan.recovery_outputs},
            )

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

    def test_single_file_restore_exports_verified_file_to_existing_directory(self) -> None:
        with normal_snapshot() as (root, target, repository, snapshot_id):
            export = root / "export"
            export.mkdir()
            repository_before = tree_file_bytes(repository)
            original_mtime_ns = (target / "notater.txt").stat().st_mtime_ns

            with patch("bildebank.snapshot_restore.os.supports_follow_symlinks", set()):
                result = restore_single_file(
                    repository,
                    snapshot_id,
                    export,
                    path="notater.txt",
                )

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.output_path.read_text(encoding="utf-8"), "familienotat\n")
            self.assertEqual(result.output_path.stat().st_mtime_ns, original_mtime_ns)
            self.assertEqual(tree_file_bytes(repository), repository_before)
            self.assertFalse((repository / REPOSITORY_LOCK_FILENAME).exists())

    def test_single_file_restore_creates_missing_export_and_relative_directories(self) -> None:
        with degraded_snapshot() as (root, _target, repository, snapshot_id, expected, _observed):
            export = root / "export"

            result = restore_single_file(
                repository,
                snapshot_id,
                export,
                path="2026/07/familie.jpg",
                variant="expected",
            )

            self.assertEqual(result.output_path, export / "2026/07/familie.jpg")
            self.assertEqual(result.output_path.read_bytes(), expected)

    def test_single_file_restore_exports_observed_variant_with_hash_suffix(self) -> None:
        with degraded_snapshot() as (root, _target, repository, snapshot_id, _expected, observed):
            export = root / "export"

            result = restore_single_file(
                repository,
                snapshot_id,
                export,
                path="2026/07/familie.jpg",
                variant="observed",
            )

            self.assertIn(f".observed-{hashlib.sha256(observed).hexdigest()[:8]}", result.output_path.name)
            self.assertEqual(result.output_path.read_bytes(), observed)

    def test_single_file_restore_exports_recovery_only_entry_by_id(self) -> None:
        with recovery_only_snapshot() as (root, repository, snapshot_id, entry_id, raw_bytes):
            export = root / "export"

            result = restore_single_file(
                repository,
                snapshot_id,
                export,
                entry_id=entry_id,
            )

            self.assertEqual(result.plan.output.variant, "observed")
            self.assertEqual(result.output_path.read_bytes(), raw_bytes)
            self.assertTrue(result.output_path.is_relative_to(export))

    def test_single_file_restore_never_overwrites_file_created_after_planning(self) -> None:
        with normal_snapshot() as (root, _target, repository, snapshot_id):
            export = root / "export"
            from bildebank.snapshot_restore import copy_verified_restore_object_exclusive

            def create_competing_file(repository_path, output, destination) -> None:
                destination.write_text("bevar\n", encoding="utf-8")
                copy_verified_restore_object_exclusive(repository_path, output, destination)

            with (
                patch(
                    "bildebank.snapshot_restore.copy_verified_restore_object_exclusive",
                    side_effect=create_competing_file,
                ),
                self.assertRaisesRegex(SnapshotStorageError, "blir ikke overskrevet"),
            ):
                restore_single_file(
                    repository,
                    snapshot_id,
                    export,
                    path="notater.txt",
                )

            self.assertEqual((export / "notater.txt").read_text(encoding="utf-8"), "bevar\n")

    def test_single_file_restore_preserves_unverified_output_when_object_is_corrupt(self) -> None:
        with normal_snapshot() as (root, _target, repository, snapshot_id):
            export = root / "export"
            plan = plan_single_file_restore(
                repository,
                snapshot_id,
                export,
                path="notater.txt",
            )
            object_path = snapshot_object_path(
                repository,
                plan.output.object.sha256,
                plan.output.object.size_bytes,
            )
            corrupt = b"x" * plan.output.object.size_bytes
            object_path.write_bytes(corrupt)

            with self.assertRaisesRegex(SnapshotStorageError, "utdatafil er bevart"):
                restore_single_file(
                    repository,
                    snapshot_id,
                    export,
                    path="notater.txt",
                )

            self.assertEqual((export / "notater.txt").read_bytes(), corrupt)
            self.assertFalse((repository / REPOSITORY_LOCK_FILENAME).exists())

    def test_single_file_restore_interrupt_preserves_partial_and_refuses_overwrite(self) -> None:
        with normal_snapshot() as (root, _target, repository, snapshot_id):
            export = root / "export"
            output = export / "notater.txt"
            repository_before = tree_file_bytes(repository)
            real_fdopen = os.fdopen
            from bildebank.snapshot_restore import copy_verified_restore_object_exclusive

            class InterruptAfterFirstRead:
                def __init__(self, stream) -> None:  # noqa: ANN001
                    self.stream = stream
                    self.reads = 0

                def __enter__(self):  # noqa: ANN204
                    self.stream.__enter__()
                    return self

                def __exit__(self, exc_type, exc, traceback):  # noqa: ANN001
                    return self.stream.__exit__(exc_type, exc, traceback)

                def read(self, size: int = -1) -> bytes:
                    if self.reads:
                        raise KeyboardInterrupt
                    self.reads += 1
                    return self.stream.read(size)

            def interrupting_fdopen(fd: int, mode: str, closefd: bool = True):  # noqa: ANN202
                stream = real_fdopen(fd, mode, closefd=closefd)
                if mode == "rb":
                    return InterruptAfterFirstRead(stream)
                return stream

            def interrupting_copy(repository_path, restore_output, destination) -> None:  # noqa: ANN001
                with (
                    patch("bildebank.snapshot_restore.COPY_CHUNK_SIZE", 4),
                    patch(
                        "bildebank.snapshot_restore.os.fdopen",
                        side_effect=interrupting_fdopen,
                    ),
                ):
                    copy_verified_restore_object_exclusive(
                        repository_path,
                        restore_output,
                        destination,
                    )

            with (
                patch(
                    "bildebank.snapshot_restore.copy_verified_restore_object_exclusive",
                    side_effect=interrupting_copy,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                restore_single_file(
                    repository,
                    snapshot_id,
                    export,
                    path="notater.txt",
                )

            self.assertEqual(output.read_bytes(), b"fami")
            self.assertEqual(tree_file_bytes(repository), repository_before)
            self.assertFalse((repository / REPOSITORY_LOCK_FILENAME).exists())

            with self.assertRaisesRegex(SnapshotStorageError, "finnes allerede"):
                restore_single_file(
                    repository,
                    snapshot_id,
                    export,
                    path="notater.txt",
                )

            self.assertEqual(output.read_bytes(), b"fami")
            self.assertFalse((repository / REPOSITORY_LOCK_FILENAME).exists())

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

    def test_cli_database_file_with_recovery_only_path_returns_three_after_restore(self) -> None:
        with database_file_recovery_only_snapshot() as (
            root,
            _target,
            repository,
            snapshot_id,
            entry_id,
            media_bytes,
        ):
            restored = root / "restored"

            code, stdout, stderr = capture_cli(
                [
                    "snapshot",
                    "restore",
                    str(repository),
                    snapshot_id,
                    str(restored),
                    "--yes",
                ]
            )

            self.assertEqual(code, 3)
            self.assertIn("Hel restore publisert", stdout)
            self.assertIn(
                f"Forventet fil mangler på ordinær plass for {entry_id}",
                stderr,
            )
            self.assertIn("Samlingen ble publisert bevisst ufullstendig", stderr)
            self.assertFalse((restored / "2026/07/familie.jpg").exists())
            recovery = next(root.glob("restored-recovery-*"))
            recovery_files = [
                path
                for path in recovery.rglob("*")
                if path.is_file() and path.name != RECOVERY_REPORT_FILENAME
            ]
            self.assertEqual(len(recovery_files), 1)
            self.assertEqual(recovery_files[0].read_bytes(), media_bytes)
            connection = sqlite3.connect(restored / db.DB_FILENAME)
            try:
                stored_path = connection.execute(
                    "SELECT target_path FROM files"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(stored_path, ("2026/07/familie.jpg",))

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

    def test_keyboard_interrupt_after_collection_rename_returns_published_restore(self) -> None:
        with degraded_snapshot() as (root, _target, repository, snapshot_id, expected, observed):
            restored = root / "restored"
            real_rename = os.rename

            def rename_then_interrupt(source, destination) -> None:  # noqa: ANN001
                real_rename(source, destination)
                if Path(source).name == "collection":
                    raise KeyboardInterrupt

            with patch(
                "bildebank.snapshot_restore.os.rename",
                side_effect=rename_then_interrupt,
            ):
                result = restore_full_snapshot(repository, snapshot_id, restored)

            self.assertEqual(result.exit_code, 0)
            self.assertEqual((restored / "2026/07/familie.jpg").read_bytes(), expected)
            assert result.published_recovery is not None
            observed_file = next(result.published_recovery.rglob("familie.observed-*.jpg"))
            self.assertEqual(observed_file.read_bytes(), observed)
            self.assertEqual(list(root.glob(".bildebank-restore-restored-*")), [])

    def test_keyboard_interrupt_after_only_recovery_rename_still_interrupts_restore(self) -> None:
        with degraded_snapshot() as (root, _target, repository, snapshot_id, _expected, _observed):
            restored = root / "restored"
            real_rename = os.rename

            def rename_then_interrupt(source, destination) -> None:  # noqa: ANN001
                real_rename(source, destination)
                if Path(source).name == "recovery":
                    raise KeyboardInterrupt

            with (
                patch(
                    "bildebank.snapshot_restore.os.rename",
                    side_effect=rename_then_interrupt,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                restore_full_snapshot(repository, snapshot_id, restored)

            self.assertFalse(restored.exists())
            recovery = next(root.glob("restored-recovery-*"))
            self.assertTrue((recovery / RECOVERY_REPORT_FILENAME).is_file())
            staging = next(root.glob(".bildebank-restore-restored-*"))
            self.assertTrue((staging / "collection").is_dir())

    def test_keyboard_interrupt_during_post_publish_cleanup_returns_restore(self) -> None:
        with normal_snapshot() as (root, _target, repository, snapshot_id):
            restored = root / "restored"
            from bildebank.snapshot_restore import cleanup_published_restore_staging

            cleanup_calls = 0

            def interrupt_cleanup_once(staging) -> tuple[str, ...]:  # noqa: ANN001
                nonlocal cleanup_calls
                cleanup_calls += 1
                if cleanup_calls == 1:
                    raise KeyboardInterrupt
                return cleanup_published_restore_staging(staging)

            with patch(
                "bildebank.snapshot_restore.cleanup_published_restore_staging",
                side_effect=interrupt_cleanup_once,
            ):
                result = restore_full_snapshot(repository, snapshot_id, restored)

            self.assertEqual(cleanup_calls, 2)
            self.assertEqual(result.exit_code, 0)
            self.assertTrue((restored / db.DB_FILENAME).is_file())
            self.assertEqual(list(root.glob(".bildebank-restore-restored-*")), [])

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

    def test_cli_single_file_restore_defaults_to_no(self) -> None:
        with normal_snapshot() as (root, _target, repository, snapshot_id):
            export = root / "export"

            with patch("builtins.input", return_value="") as prompt:
                code, stdout, stderr = capture_cli(
                    [
                        "snapshot",
                        "restore-file",
                        str(repository),
                        snapshot_id,
                        str(export),
                        "--path",
                        "notater.txt",
                    ]
                )

            self.assertEqual(code, 1)
            self.assertIn("Plan for enkeltfil-restore", stdout)
            self.assertIn("Enkeltfil-restore avbrutt", stderr)
            self.assertFalse(export.exists())
            prompt.assert_called_once_with("Eksportere filen som vist i planen? [j/N] ")

    def test_cli_single_file_restore_accepts_simple_confirmation(self) -> None:
        with normal_snapshot() as (root, _target, repository, snapshot_id):
            export = root / "export"

            with patch("builtins.input", return_value=" J "):
                code, stdout, stderr = capture_cli(
                    [
                        "snapshot",
                        "restore-file",
                        str(repository),
                        snapshot_id,
                        str(export),
                        "--path",
                        "notater.txt",
                    ]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Enkeltfil-restore fullført", stdout)
            self.assertEqual((export / "notater.txt").read_text(encoding="utf-8"), "familienotat\n")

    def test_cli_single_file_restore_yes_skips_prompt(self) -> None:
        with normal_snapshot() as (root, _target, repository, snapshot_id):
            export = root / "export"

            with patch("builtins.input", side_effect=AssertionError("skal ikke spørre")):
                code, stdout, stderr = capture_cli(
                    [
                        "snapshot",
                        "restore-file",
                        str(repository),
                        snapshot_id,
                        str(export),
                        "--path",
                        "notater.txt",
                        "--yes",
                    ]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Enkeltfil-restore fullført", stdout)
            self.assertTrue((export / "notater.txt").is_file())


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


class database_file_recovery_only_snapshot:
    def __init__(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()

    def __enter__(self) -> tuple[Path, Path, Path, str, str, bytes]:
        root = Path(self._tempdir.name)
        target = root / "collection"
        repository = root / "repository"
        db.init_database(target)
        media_bytes = b"original"
        relative_path = "2026/07/familie.jpg"
        media = target / relative_path
        media.parent.mkdir(parents=True)
        media.write_bytes(media_bytes)
        insert_database_file(
            target,
            relative_path,
            hashlib.sha256(media_bytes).hexdigest(),
            len(media_bytes),
        )

        def mark_media_path_unsafe(path: str) -> str | None:
            if path == relative_path:
                return None
            return portable_path_key(path)

        with patch(
            "bildebank.snapshot_builder.portable_path_key",
            side_effect=mark_media_path_unsafe,
        ):
            result = create_snapshot(target, repository)

        assert result.status == "degraded"
        entries = [
            json.loads(line)
            for line in (result.published.snapshot_dir / "files.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        recovery_entry = next(
            entry
            for entry in entries
            if entry["original_path_display"] == relative_path
        )
        assert recovery_entry["restore_kind"] == "recovery_only"
        assert recovery_entry["expected"] is not None
        return (
            root,
            target,
            repository,
            result.published.snapshot_id,
            recovery_entry["entry_id"],
            media_bytes,
        )

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self._tempdir.cleanup()


class recovery_only_snapshot:
    def __init__(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()

    def __enter__(self) -> tuple[Path, Path, str, str, bytes]:
        root = Path(self._tempdir.name)
        target = root / "collection"
        repository = root / "repository"
        db.init_database(target)
        raw_bytes = b"ikke en sqlite-database"
        (target / ".bilder-openclip.sqlite3").write_bytes(raw_bytes)
        result = create_snapshot(target, repository)
        assert result.status == "degraded"
        entries = [
            json.loads(line)
            for line in (result.published.snapshot_dir / "files.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        recovery_entry = next(entry for entry in entries if entry["restore_kind"] == "recovery_only")
        return root, repository, result.published.snapshot_id, recovery_entry["entry_id"], raw_bytes

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


def create_sqlite_database(path: Path, *, schema_version: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO meta VALUES('schema_version', ?)", (str(schema_version),))
        connection.execute("CREATE TABLE content(value TEXT NOT NULL)")
        connection.execute("INSERT INTO content VALUES('bevar')")
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
