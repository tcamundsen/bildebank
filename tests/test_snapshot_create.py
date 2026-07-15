from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bildebank import db
from bildebank.snapshot import REPOSITORY_LOCK_FILENAME
from bildebank.snapshot_create import (
    create_snapshot,
    validate_existing_recovery_repository,
)
from bildebank.snapshot_repository import RepositoryLockError, SnapshotStorageError


class SnapshotCreateTests(unittest.TestCase):
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
