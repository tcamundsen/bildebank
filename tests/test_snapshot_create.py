from __future__ import annotations

import errno
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.snapshot import REPOSITORY_LOCK_FILENAME
from bildebank.snapshot_check import check_snapshot_repository
from bildebank.snapshot_create import (
    create_snapshot,
    validate_existing_recovery_repository,
)
from bildebank.snapshot_repository import COPY_CHUNK_SIZE, RepositoryLockError, SnapshotStorageError
from bildebank.target_lock import LOCK_FILENAME


class SnapshotCreateTests(unittest.TestCase):
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
