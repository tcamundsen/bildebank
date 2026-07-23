from __future__ import annotations

import errno
import json
import platform
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.snapshot import (
    REPOSITORY_LOCK_FILENAME,
    REPOSITORY_METADATA_FILENAME,
    RepositoryBindingChange,
    plan_snapshot,
)
from bildebank.snapshot_check import check_snapshot_repository
from bildebank.snapshot_create import (
    create_snapshot,
    validate_existing_recovery_repository,
)
from bildebank.snapshot_repository import COPY_CHUNK_SIZE, RepositoryLockError, SnapshotStorageError
from bildebank.snapshot_progress import SnapshotCancelled, SnapshotCreateProgress
from bildebank.target_lock import LOCK_FILENAME


class SnapshotCreateTests(unittest.TestCase):
    def test_moved_collection_requires_exact_confirmation_from_read_only_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "collection"
            moved_target = root / "moved-collection"
            repository = root / "repository"
            db.init_database(target)
            first = create_snapshot(target, repository)
            first_before = tree_file_bytes(first.published.snapshot_dir)
            repository_before = tree_file_bytes(repository)
            target.rename(moved_target)

            plan = plan_snapshot(moved_target, repository)

            self.assertIsNotNone(plan.binding_change)
            assert plan.binding_change is not None
            self.assertEqual(
                plan.binding_change.previous_collection_path,
                str(target.resolve()),
            )
            self.assertEqual(
                plan.binding_change.current_collection_path,
                str(moved_target.resolve()),
            )
            self.assertEqual(tree_file_bytes(repository), repository_before)

            with self.assertRaisesRegex(SnapshotStorageError, "eksplisitt bekreftelse"):
                create_snapshot(moved_target, repository)

            result = create_snapshot(
                moved_target,
                repository,
                confirmed_binding_change=plan.binding_change,
            )

            metadata = json.loads(
                (repository / REPOSITORY_METADATA_FILENAME).read_text(encoding="utf-8")
            )
            self.assertEqual(result.status, "complete")
            self.assertEqual(
                metadata["last_confirmed_source"]["collection_path"],
                str(moved_target.resolve()),
            )
            self.assertEqual(
                metadata["last_confirmed_source"]["machine_name"],
                platform.node(),
            )
            self.assertEqual(len(list((repository / "snapshots").iterdir())), 2)
            self.assertEqual(tree_file_bytes(first.published.snapshot_dir), first_before)

    def test_binding_confirmation_preserves_repository_identity_and_unknown_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "collection"
            moved_target = root / "moved-collection"
            repository = root / "repository"
            db.init_database(target)
            create_snapshot(target, repository)
            metadata_path = repository / REPOSITORY_METADATA_FILENAME
            metadata_before = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata_before["future_extension"] = {"bevar": ["alt", 123]}
            metadata_path.write_text(
                json.dumps(metadata_before, ensure_ascii=False),
                encoding="utf-8",
            )
            target.rename(moved_target)
            plan = plan_snapshot(moved_target, repository)
            assert plan.binding_change is not None

            create_snapshot(
                moved_target,
                repository,
                confirmed_binding_change=plan.binding_change,
            )

            metadata_after = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(
                metadata_after["collection_id"],
                metadata_before["collection_id"],
            )
            self.assertEqual(
                metadata_after["repository_id"],
                metadata_before["repository_id"],
            )
            self.assertEqual(
                metadata_after["future_extension"],
                metadata_before["future_extension"],
            )
            self.assertEqual(
                metadata_after["created_at"],
                metadata_before["created_at"],
            )

    def test_machine_change_uses_the_same_explicit_confirmation_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "collection"
            repository = root / "repository"
            db.init_database(target)
            create_snapshot(target, repository)
            metadata_path = repository / REPOSITORY_METADATA_FILENAME
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["last_confirmed_source"]["machine_name"] = "ANNEN-MASKIN"
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False),
                encoding="utf-8",
            )
            repository_before = tree_file_bytes(repository)

            plan = plan_snapshot(target, repository)

            self.assertIsNotNone(plan.binding_change)
            assert plan.binding_change is not None
            self.assertEqual(
                plan.binding_change.previous_collection_path,
                plan.binding_change.current_collection_path,
            )
            self.assertEqual(
                plan.binding_change.previous_machine_name,
                "ANNEN-MASKIN",
            )
            self.assertEqual(tree_file_bytes(repository), repository_before)
            with self.assertRaisesRegex(SnapshotStorageError, "eksplisitt bekreftelse"):
                create_snapshot(target, repository)

            result = create_snapshot(
                target,
                repository,
                confirmed_binding_change=plan.binding_change,
            )

            self.assertEqual(result.status, "complete")
            updated = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(
                updated["last_confirmed_source"]["machine_name"],
                platform.node(),
            )

    def test_stale_binding_confirmation_is_rejected_without_repository_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "collection"
            first_move = root / "first-move"
            second_move = root / "second-move"
            repository = root / "repository"
            db.init_database(target)
            create_snapshot(target, repository)
            target.rename(first_move)
            plan = plan_snapshot(first_move, repository)
            assert plan.binding_change is not None
            first_move.rename(second_move)
            repository_before = tree_file_bytes(repository)

            with self.assertRaisesRegex(SnapshotStorageError, "utdatert"):
                create_snapshot(
                    second_move,
                    repository,
                    confirmed_binding_change=plan.binding_change,
                )

            self.assertEqual(tree_file_bytes(repository), repository_before)
            self.assertFalse((second_move / LOCK_FILENAME).exists())

    def test_binding_confirmation_cannot_rebind_recovery_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "collection"
            moved_target = root / "moved-collection"
            repository = root / "repository"
            db.init_database(target)
            create_snapshot(target, repository)
            target.rename(moved_target)
            plan = plan_snapshot(moved_target, repository)
            assert plan.binding_change is not None
            (moved_target / db.DB_FILENAME).write_bytes(b"skadet hoveddatabase")
            repository_before = tree_file_bytes(repository)

            with self.assertRaisesRegex(SnapshotStorageError, "annen samlingssti"):
                create_snapshot(
                    moved_target,
                    repository,
                    confirmed_binding_change=plan.binding_change,
                )

            self.assertEqual(tree_file_bytes(repository), repository_before)
            self.assertFalse((moved_target / LOCK_FILENAME).exists())

    def test_binding_confirmation_rejects_different_collection_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "original"
            other = root / "other"
            repository = root / "repository"
            db.init_database(original)
            db.init_database(other)
            create_snapshot(original, repository)
            repository_before = tree_file_bytes(repository)
            machine = platform.node()
            forged_confirmation = RepositoryBindingChange(
                previous_collection_path=str(original.resolve()),
                current_collection_path=str(other.resolve()),
                previous_machine_name=machine,
                current_machine_name=machine,
            )

            with self.assertRaisesRegex(SnapshotStorageError, "annen bildesamling"):
                create_snapshot(
                    other,
                    repository,
                    confirmed_binding_change=forged_confirmation,
                )

            self.assertEqual(tree_file_bytes(repository), repository_before)
            self.assertFalse((other / LOCK_FILENAME).exists())

    def test_failed_atomic_binding_replace_preserves_previous_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "collection"
            moved_target = root / "moved-collection"
            repository = root / "repository"
            db.init_database(target)
            create_snapshot(target, repository)
            target.rename(moved_target)
            plan = plan_snapshot(moved_target, repository)
            assert plan.binding_change is not None
            metadata_path = repository / REPOSITORY_METADATA_FILENAME
            metadata_before = metadata_path.read_bytes()

            with (
                patch(
                    "bildebank.snapshot_repository.os.replace",
                    side_effect=OSError(errno.EIO, "simulert replace-feil"),
                ),
                self.assertRaises(OSError),
            ):
                create_snapshot(
                    moved_target,
                    repository,
                    confirmed_binding_change=plan.binding_change,
                )

            self.assertEqual(metadata_path.read_bytes(), metadata_before)
            self.assertEqual(
                list(repository.glob(f"{REPOSITORY_METADATA_FILENAME}.tmp-*")),
                [],
            )
            self.assertFalse((moved_target / LOCK_FILENAME).exists())

    def test_reports_inventory_file_bytes_databases_and_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "collection"
            repository = root / "repository"
            db.init_database(target)
            content = b"x" * (COPY_CHUNK_SIZE * 2 + 17)
            (target / "stor-ukjent-fil.bin").write_bytes(content)
            progress = []

            result = create_snapshot(target, repository, progress=progress.append)

            self.assertEqual(result.status, "complete")
            self.assertEqual(
                {item.stage for item in progress},
                {"inventory", "files", "databases", "publish"},
            )
            file_progress = [item for item in progress if item.stage == "files"]
            self.assertEqual(file_progress[0].completed_objects, 0)
            self.assertEqual(file_progress[0].total_objects, 1)
            self.assertEqual(file_progress[0].total_bytes, len(content))
            self.assertTrue(
                any(0 < item.completed_bytes < len(content) for item in file_progress)
            )
            self.assertEqual(file_progress[-1].completed_objects, 1)
            self.assertEqual(file_progress[-1].completed_bytes, len(content))
            database_progress = [item for item in progress if item.stage == "databases"]
            self.assertEqual(database_progress[0].completed_objects, 0)
            self.assertEqual(database_progress[-1].completed_objects, 1)
            self.assertEqual(
                [item.completed_objects for item in progress if item.stage == "publish"],
                [0, 1],
            )

    def test_interrupted_object_copy_preserves_previous_snapshot_and_incomplete_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "collection"
            repository = root / "repository"
            db.init_database(target)
            first = create_snapshot(target, repository)
            first_before = tree_file_bytes(first.published.snapshot_dir)
            (target / "avbryt-kopiering.txt").write_bytes(b"delvis kopi som skal avbrytes")

            def interrupt_copy(source, destination, digest):  # noqa: ANN001, ANN202
                chunk = source.read(6)
                destination.write(chunk)
                digest.update(chunk)
                raise KeyboardInterrupt

            with (
                patch("bildebank.snapshot_repository.copy_and_hash", side_effect=interrupt_copy),
                self.assertRaises(KeyboardInterrupt),
            ):
                create_snapshot(target, repository)

            self.assertEqual(len(list((repository / "snapshots").iterdir())), 1)
            self.assertEqual(tree_file_bytes(first.published.snapshot_dir), first_before)
            incomplete_runs = list((repository / "incomplete").iterdir())
            self.assertEqual(len(incomplete_runs), 1)
            self.assertIn(b"delvis", tree_file_bytes(incomplete_runs[0]).values())
            self.assertFalse((repository / REPOSITORY_LOCK_FILENAME).exists())
            self.assertFalse((target / LOCK_FILENAME).exists())

            checked = check_snapshot_repository(repository, full=True)
            self.assertEqual(checked.exit_code, 0)
            self.assertTrue(checked.completed)
            self.assertEqual(len(checked.snapshots), 1)
            self.assertEqual(len(checked.incomplete_runs), 1)
            self.assertEqual(tree_file_bytes(first.published.snapshot_dir), first_before)

    def test_controlled_cancel_during_copy_publishes_nothing_and_releases_locks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "collection"
            repository = root / "repository"
            db.init_database(target)
            first = create_snapshot(target, repository)
            first_before = tree_file_bytes(first.published.snapshot_dir)
            objects_before = tree_file_bytes(repository / "objects")
            (target / "stor-fil.bin").write_bytes(b"x" * (COPY_CHUNK_SIZE * 2 + 17))
            cancel_requested = False

            def report_progress(progress: SnapshotCreateProgress) -> None:
                nonlocal cancel_requested
                if progress.stage == "files" and progress.completed_bytes >= COPY_CHUNK_SIZE:
                    cancel_requested = True

            with self.assertRaises(SnapshotCancelled):
                create_snapshot(
                    target,
                    repository,
                    progress=report_progress,
                    should_cancel=lambda: cancel_requested,
                )

            self.assertEqual(len(list((repository / "snapshots").iterdir())), 1)
            self.assertEqual(tree_file_bytes(first.published.snapshot_dir), first_before)
            self.assertEqual(tree_file_bytes(repository / "objects"), objects_before)
            self.assertEqual(len(list((repository / "incomplete").iterdir())), 1)
            self.assertFalse((repository / REPOSITORY_LOCK_FILENAME).exists())
            self.assertFalse((target / LOCK_FILENAME).exists())

    def test_no_space_during_object_copy_publishes_nothing_and_preserves_previous_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "collection"
            repository = root / "repository"
            db.init_database(target)
            first = create_snapshot(target, repository)
            first_before = tree_file_bytes(first.published.snapshot_dir)
            objects_before = tree_file_bytes(repository / "objects")
            (target / "for-stor-for-ledig-plass.txt").write_bytes(b"nytt objekt")

            no_space = OSError(errno.ENOSPC, "simulert fullt medium")
            with (
                patch("bildebank.snapshot_repository.copy_and_hash", side_effect=no_space),
                self.assertRaises(OSError) as raised,
            ):
                create_snapshot(target, repository)

            self.assertEqual(raised.exception.errno, errno.ENOSPC)
            self.assertEqual(len(list((repository / "snapshots").iterdir())), 1)
            self.assertEqual(tree_file_bytes(first.published.snapshot_dir), first_before)
            self.assertEqual(tree_file_bytes(repository / "objects"), objects_before)
            self.assertEqual(len(list((repository / "incomplete").iterdir())), 1)
            self.assertFalse((repository / REPOSITORY_LOCK_FILENAME).exists())
            self.assertFalse((target / LOCK_FILENAME).exists())

            checked = check_snapshot_repository(repository, full=True)
            self.assertEqual(checked.exit_code, 0)
            self.assertEqual(len(checked.snapshots), 1)
            self.assertEqual(len(checked.incomplete_runs), 1)

    def test_recovery_preflight_validates_existing_binding_without_writing_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "collection"
            repository = root / "repository"
            db.init_database(target)
            create_snapshot(target, repository)
            (target / db.DB_FILENAME).write_bytes(b"skadet hoveddatabase")
            repository_before = tree_file_bytes(repository)

            validated = validate_existing_recovery_repository(target, repository)

            self.assertEqual(validated, repository.resolve())
            self.assertEqual(tree_file_bytes(repository), repository_before)
            self.assertFalse((repository / REPOSITORY_LOCK_FILENAME).exists())

    def test_missing_main_database_publishes_recovery_to_previously_bound_repository(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "collection"
            repository = root / "repository"
            db.init_database(target)
            create_snapshot(target, repository)
            new_file = target / "ny-fil.jpg"
            new_file.write_bytes(b"innhold som bare finnes etter forrige snapshot")
            (target / db.DB_FILENAME).unlink()

            result = create_snapshot(target, repository)

            self.assertEqual(result.status, "recovery")
            self.assertEqual(result.exit_code, 4)
            recovered = next(item for item in result.build.files if item.path == "ny-fil.jpg")
            self.assertEqual(recovered.integrity_status, "ok")
            self.assertIsNotNone(recovered.object)
            self.assertFalse((target / db.DB_FILENAME).exists())
            self.assertEqual(len(list((repository / "snapshots").iterdir())), 2)

    def test_recovery_preflight_accepts_missing_main_database_without_writing_repository(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "collection"
            repository = root / "repository"
            db.init_database(target)
            create_snapshot(target, repository)
            (target / db.DB_FILENAME).unlink()
            repository_before = tree_file_bytes(repository)

            validated = validate_existing_recovery_repository(target, repository)

            self.assertEqual(validated, repository.resolve())
            self.assertEqual(tree_file_bytes(repository), repository_before)
            self.assertFalse((repository / REPOSITORY_LOCK_FILENAME).exists())

    def test_recovery_preflight_does_not_create_missing_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "collection"
            repository = root / "repository"
            db.init_database(target)
            (target / db.DB_FILENAME).write_bytes(b"skadet hoveddatabase")

            with self.assertRaisesRegex(SnapshotStorageError, "allerede initialisert"):
                validate_existing_recovery_repository(target, repository)

            self.assertFalse(repository.exists())

    def test_recovery_preflight_rejects_active_repository_lock_without_changing_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "collection"
            repository = root / "repository"
            db.init_database(target)
            create_snapshot(target, repository)
            (target / db.DB_FILENAME).write_bytes(b"skadet hoveddatabase")
            lock_path = repository / REPOSITORY_LOCK_FILENAME
            lock_path.write_text("opptatt\n", encoding="utf-8")

            with self.assertRaisesRegex(RepositoryLockError, "låst"):
                validate_existing_recovery_repository(target, repository)

            self.assertEqual(lock_path.read_text(encoding="utf-8"), "opptatt\n")


def tree_file_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
