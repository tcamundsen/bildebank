from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from bildebank.snapshot import REPOSITORY_LOCK_FILENAME, REPOSITORY_METADATA_FILENAME, plan_snapshot
from bildebank.snapshot_repository import (
    RepositoryLock,
    RepositoryLockError,
    SnapshotStorageError,
    SourceDatabaseError,
    backup_sqlite_database,
    canonical_json_bytes,
    create_staging_run,
    initialize_repository,
    store_verified_file,
)
from tests.cli_helpers import run_cli


class SnapshotRepositoryTests(unittest.TestCase):
    def test_repository_lock_is_exclusive_and_removed_after_controlled_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp) / "repository"
            repository.mkdir()

            with RepositoryLock(repository, command="snapshot create"):
                lock_path = repository / REPOSITORY_LOCK_FILENAME
                lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(lock_data["command"], "snapshot create")
                self.assertIsInstance(lock_data["pid"], int)
                with self.assertRaises(RepositoryLockError):
                    with RepositoryLock(repository, command="snapshot check"):
                        self.fail("En ny repositorylås skal ikke kunne tas")

            self.assertFalse((repository / REPOSITORY_LOCK_FILENAME).exists())

    def test_dry_run_rejects_existing_repository_lock_without_removing_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            repository.mkdir()
            lock_path = repository / REPOSITORY_LOCK_FILENAME
            lock_path.write_text("opptatt\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "låst"):
                plan_snapshot(target, repository)

            self.assertEqual(lock_path.read_text(encoding="utf-8"), "opptatt\n")

    def test_initialization_writes_canonical_metadata_and_layout_under_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "Familiebilder"
            repository = root / "repository"
            repository.mkdir()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            collection_id = read_collection_id(target)
            repository_id = str(uuid.uuid4())

            with RepositoryLock(repository, command="snapshot create"):
                metadata = initialize_repository(
                    repository,
                    target,
                    collection_id,
                    repository_id=repository_id,
                    created_at="2026-07-15T12:00:00Z",
                )

            metadata_path = repository / REPOSITORY_METADATA_FILENAME
            self.assertEqual(metadata_path.read_bytes(), canonical_json_bytes(metadata))
            self.assertEqual(metadata["repository_id"], repository_id)
            self.assertTrue((repository / "objects" / "sha256").is_dir())
            self.assertTrue((repository / "snapshots").is_dir())
            self.assertTrue((repository / "incomplete").is_dir())
            self.assertTrue((repository / "README.txt").is_file())
            self.assertEqual(plan_snapshot(target, repository).repository_state, "existing")

    def test_initialization_refuses_unexpected_content_without_changing_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            repository.mkdir()
            unexpected = repository / "mine-data.txt"
            unexpected.write_text("bevar\n", encoding="utf-8")
            self.assertEqual(run_cli(["create", str(target)]), 0)

            with RepositoryLock(repository, command="snapshot create"):
                with self.assertRaisesRegex(SnapshotStorageError, "ikke tomt"):
                    initialize_repository(repository, target, read_collection_id(target))

            self.assertEqual(unexpected.read_text(encoding="utf-8"), "bevar\n")
            self.assertFalse((repository / REPOSITORY_METADATA_FILENAME).exists())

    def test_staging_runs_are_unique_and_existing_runs_are_never_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repository"
            repository.mkdir()
            source = root / "source"
            source.mkdir()
            run_id = str(uuid.uuid4())

            with RepositoryLock(repository, command="snapshot create"):
                initialize_repository(repository, source, str(uuid.uuid4()))
                first = create_staging_run(repository, run_id=run_id)
                marker = first / "bevar.txt"
                marker.write_text("ikke rør\n", encoding="utf-8")

                with self.assertRaisesRegex(SnapshotStorageError, "finnes allerede"):
                    create_staging_run(repository, run_id=run_id)
            self.assertEqual(marker.read_text(encoding="utf-8"), "ikke rør\n")

            with self.assertRaisesRegex(SnapshotStorageError, "repositorylåsen"):
                create_staging_run(repository)

    def test_store_verified_file_hashes_copy_and_reuses_published_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repository"
            repository.mkdir()
            collection = root / "collection"
            collection.mkdir()
            source = root / "familie.jpg"
            content = b"unikt familiebilde"
            source.write_bytes(content)

            with RepositoryLock(repository, command="snapshot create"):
                initialize_repository(repository, collection, str(uuid.uuid4()))
                staging = create_staging_run(repository)
                first = store_verified_file(repository, staging, source)
                second = store_verified_file(repository, staging, source)

            self.assertFalse(first.reused)
            self.assertTrue(second.reused)
            self.assertEqual(first.reference.sha256, hashlib.sha256(content).hexdigest())
            self.assertEqual(first.reference.size_bytes, len(content))
            object_path = (
                repository
                / "objects"
                / "sha256"
                / first.reference.sha256[:2]
                / first.reference.sha256[2:4]
                / f"{first.reference.sha256}-{len(content)}"
            )
            self.assertEqual(object_path.read_bytes(), content)
            self.assertEqual(first.reference.as_json()["size_bytes"], str(len(content)))

    def test_store_verified_file_never_overwrites_wrong_size_existing_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repository"
            repository.mkdir()
            collection = root / "collection"
            collection.mkdir()
            source = root / "familie.jpg"
            content = b"familiebilde"
            source.write_bytes(content)
            sha256 = hashlib.sha256(content).hexdigest()
            object_path = (
                repository
                / "objects"
                / "sha256"
                / sha256[:2]
                / sha256[2:4]
                / f"{sha256}-{len(content)}"
            )

            with RepositoryLock(repository, command="snapshot create"):
                initialize_repository(repository, collection, str(uuid.uuid4()))
                staging = create_staging_run(repository)
                object_path.parent.mkdir(parents=True, exist_ok=True)
                object_path.write_bytes(b"feil")

                with self.assertRaisesRegex(SnapshotStorageError, "feil størrelse"):
                    store_verified_file(repository, staging, source)

            self.assertEqual(object_path.read_bytes(), b"feil")

    def test_store_verified_file_rejects_non_repository_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repository"
            repository.mkdir()
            outside = root / "outside"
            outside.mkdir()
            source = root / "image.jpg"
            source.write_bytes(b"image")

            with self.assertRaisesRegex(SnapshotStorageError, "ikke under"):
                store_verified_file(repository, outside, source)

    def test_sqlite_backup_api_captures_wal_content_and_publishes_valid_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repository"
            repository.mkdir()
            collection = root / "collection"
            collection.mkdir()
            database = root / "source.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                connection.execute("INSERT INTO meta VALUES('schema_version', '7')")
                connection.execute("CREATE TABLE photos(name TEXT NOT NULL)")
                connection.execute("INSERT INTO photos VALUES('familie.jpg')")
                connection.commit()

                with RepositoryLock(repository, command="snapshot create"):
                    initialize_repository(repository, collection, str(uuid.uuid4()))
                    staging = create_staging_run(repository)
                    backup = backup_sqlite_database(repository, staging, database)
            finally:
                connection.close()

            object_path = (
                repository
                / "objects"
                / "sha256"
                / backup.object.reference.sha256[:2]
                / backup.object.reference.sha256[2:4]
                / f"{backup.object.reference.sha256}-{backup.object.reference.size_bytes}"
            )
            restored = sqlite3.connect(object_path)
            try:
                row = restored.execute("SELECT name FROM photos").fetchone()
                integrity = restored.execute("PRAGMA integrity_check").fetchone()
            finally:
                restored.close()
            self.assertEqual(row, ("familie.jpg",))
            self.assertEqual(integrity, ("ok",))
            self.assertEqual(backup.schema_version, 7)

    def test_sqlite_backup_reports_corrupt_source_without_publishing_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repository"
            repository.mkdir()
            collection = root / "collection"
            collection.mkdir()
            database = root / "source.sqlite3"
            database.write_bytes(b"dette er ikke SQLite")

            with RepositoryLock(repository, command="snapshot create"):
                initialize_repository(repository, collection, str(uuid.uuid4()))
                staging = create_staging_run(repository)
                with self.assertRaises(SourceDatabaseError):
                    backup_sqlite_database(repository, staging, database)

            object_files = list((repository / "objects" / "sha256").rglob("*"))
            self.assertEqual([path for path in object_files if path.is_file()], [])


def read_collection_id(target: Path) -> str:
    connection = sqlite3.connect(target / ".bilder.sqlite3")
    try:
        row = connection.execute("SELECT value FROM meta WHERE key = 'collection_id'").fetchone()
        assert row is not None
        return str(row[0])
    finally:
        connection.close()
