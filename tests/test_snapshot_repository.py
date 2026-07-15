from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from bildebank.snapshot import REPOSITORY_LOCK_FILENAME, REPOSITORY_METADATA_FILENAME, plan_snapshot
from bildebank.snapshot_repository import (
    RepositoryLock,
    RepositoryLockError,
    SnapshotStorageError,
    ExpectedFile,
    ObjectReference,
    SnapshotDatabaseRecord,
    SnapshotFileRecord,
    SourceDatabaseError,
    backup_sqlite_database,
    canonical_json_bytes,
    create_staging_run,
    initialize_repository,
    publish_snapshot,
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

    def test_publish_snapshot_writes_canonical_v1_files_in_deterministic_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repository"
            repository.mkdir()
            collection = root / "collection"
            collection.mkdir()
            collection_id = str(uuid.uuid4())
            repository_id = str(uuid.uuid4())
            snapshot_id = str(uuid.uuid4())
            first_source = root / "b.jpg"
            second_source = root / "a.jpg"
            first_source.write_bytes(b"bilde b")
            second_source.write_bytes(b"bilde a")

            with RepositoryLock(repository, command="snapshot create"):
                initialize_repository(
                    repository,
                    collection,
                    collection_id,
                    repository_id=repository_id,
                    created_at="2026-07-15T10:00:00Z",
                )
                staging = create_staging_run(repository)
                first = store_verified_file(repository, staging, first_source)
                second = store_verified_file(repository, staging, second_source)
                published = publish_snapshot(
                    repository,
                    staging,
                    collection_id=collection_id,
                    repository_id=repository_id,
                    status="complete",
                    collection_identity_source="database",
                    started_at="2026-07-15T12:00:00Z",
                    completed_at="2026-07-15T12:01:02Z",
                    files=(
                        SnapshotFileRecord(
                            path="2026/07/b.jpg",
                            original_path_display="2026/07/b.jpg",
                            restore_kind="normal",
                            integrity_status="ok",
                            expected=ExpectedFile(first.reference.sha256, first.reference.size_bytes),
                            object=first.reference,
                            mtime_ns=first.source_mtime_ns,
                        ),
                        SnapshotFileRecord(
                            path="2026/07/a.jpg",
                            original_path_display="2026/07/a.jpg",
                            restore_kind="normal",
                            integrity_status="ok",
                            object=second.reference,
                            mtime_ns=second.source_mtime_ns,
                        ),
                    ),
                    databases=(main_database_record(first.reference),),
                    schema_versions={"main": 14},
                    exclusions=("thumbnails",),
                    note="Før ferien",
                    snapshot_id=snapshot_id,
                )

                self.assertTrue((repository / REPOSITORY_LOCK_FILENAME).exists())

            expected_directory = repository / "snapshots" / f"2026-07-15T120102Z-{snapshot_id}"
            self.assertEqual(published.snapshot_dir, expected_directory)
            self.assertEqual(published.entry_count, 2)
            self.assertFalse(staging.exists())
            lines = expected_directory.joinpath("files.jsonl").read_text(encoding="utf-8").splitlines()
            entries = [json.loads(line) for line in lines]
            self.assertEqual([entry["path"] for entry in entries], ["2026/07/a.jpg", "2026/07/b.jpg"])
            self.assertEqual([entry["entry_id"] for entry in entries], ["e-000000000001", "e-000000000002"])
            self.assertEqual(
                expected_directory.joinpath("files.jsonl").read_bytes(),
                b"".join(canonical_json_bytes(entry) for entry in entries),
            )
            manifest_bytes = expected_directory.joinpath("manifest.json").read_bytes()
            manifest = json.loads(manifest_bytes)
            commit = json.loads(expected_directory.joinpath("commit.json").read_bytes())
            self.assertEqual(manifest["snapshot_id"], snapshot_id)
            self.assertEqual(manifest["files_jsonl"]["entry_count"], "2")
            self.assertEqual(commit["manifest"]["sha256"], hashlib.sha256(manifest_bytes).hexdigest())
            self.assertEqual(
                commit["files_jsonl"]["sha256"],
                hashlib.sha256(expected_directory.joinpath("files.jsonl").read_bytes()).hexdigest(),
            )

    def test_publish_snapshot_refuses_missing_object_before_writing_snapshot_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository, collection_id, repository_id, staging = initialized_staging(root)
            missing = ObjectReference("a" * 64, 12)

            with RepositoryLock(repository, command="snapshot create"):
                with self.assertRaisesRegex(SnapshotStorageError, "Snapshotobjekt"):
                    publish_snapshot(
                        repository,
                        staging,
                        collection_id=collection_id,
                        repository_id=repository_id,
                        status="complete",
                        collection_identity_source="database",
                        started_at="2026-07-15T12:00:00Z",
                        completed_at="2026-07-15T12:01:00Z",
                        files=(normal_file_record("2026/07/a.jpg", missing),),
                        databases=(main_database_record(missing),),
                        schema_versions={"main": 14},
                    )

            self.assertFalse((staging / "snapshot").exists())
            self.assertEqual(list((repository / "snapshots").iterdir()), [])

    def test_publish_failure_leaves_complete_staging_and_no_published_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repository"
            repository.mkdir()
            collection = root / "collection"
            collection.mkdir()
            collection_id = str(uuid.uuid4())
            repository_id = str(uuid.uuid4())
            source = root / "a.jpg"
            source.write_bytes(b"a")

            with RepositoryLock(repository, command="snapshot create"):
                initialize_repository(repository, collection, collection_id, repository_id=repository_id)
                staging = create_staging_run(repository)
                stored = store_verified_file(repository, staging, source)
                with patch("bildebank.snapshot_repository.os.rename", side_effect=OSError("avbrutt")):
                    with self.assertRaisesRegex(SnapshotStorageError, "atomisk"):
                        publish_snapshot(
                            repository,
                            staging,
                            collection_id=collection_id,
                            repository_id=repository_id,
                            status="complete",
                            collection_identity_source="database",
                            started_at="2026-07-15T12:00:00Z",
                            completed_at="2026-07-15T12:01:00Z",
                            files=(normal_file_record("2026/07/a.jpg", stored.reference),),
                            databases=(main_database_record(stored.reference),),
                            schema_versions={"main": 14},
                        )

            self.assertTrue((staging / "snapshot" / "files.jsonl").is_file())
            self.assertTrue((staging / "snapshot" / "manifest.json").is_file())
            self.assertTrue((staging / "snapshot" / "commit.json").is_file())
            self.assertEqual(list((repository / "snapshots").iterdir()), [])

    def test_publishing_new_snapshot_never_changes_or_overwrites_older_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repository"
            repository.mkdir()
            collection = root / "collection"
            collection.mkdir()
            collection_id = str(uuid.uuid4())
            repository_id = str(uuid.uuid4())
            first_snapshot_id = str(uuid.uuid4())
            source = root / "a.jpg"
            source.write_bytes(b"a")

            with RepositoryLock(repository, command="snapshot create"):
                initialize_repository(repository, collection, collection_id, repository_id=repository_id)
                first_staging = create_staging_run(repository)
                stored = store_verified_file(repository, first_staging, source)
                first = publish_snapshot(
                    repository,
                    first_staging,
                    collection_id=collection_id,
                    repository_id=repository_id,
                    status="complete",
                    collection_identity_source="database",
                    started_at="2026-07-15T12:00:00Z",
                    completed_at="2026-07-15T12:01:00Z",
                    files=(),
                    databases=(main_database_record(stored.reference),),
                    schema_versions={"main": 14},
                    snapshot_id=first_snapshot_id,
                )
                first_before = tree_file_bytes(first.snapshot_dir)
                self.assertEqual(first.snapshot_dir.joinpath("files.jsonl").read_bytes(), b"")

                second_staging = create_staging_run(repository)
                second = publish_snapshot(
                    repository,
                    second_staging,
                    collection_id=collection_id,
                    repository_id=repository_id,
                    status="complete",
                    collection_identity_source="database",
                    started_at="2026-07-15T12:02:00Z",
                    completed_at="2026-07-15T12:03:00Z",
                    files=(normal_file_record("2026/07/a.jpg", stored.reference),),
                    databases=(main_database_record(stored.reference),),
                    schema_versions={"main": 14},
                )

                collision_staging = create_staging_run(repository)
                with self.assertRaisesRegex(SnapshotStorageError, "ikke overskrevet"):
                    publish_snapshot(
                        repository,
                        collision_staging,
                        collection_id=collection_id,
                        repository_id=repository_id,
                        status="complete",
                        collection_identity_source="database",
                        started_at="2026-07-15T12:00:00Z",
                        completed_at="2026-07-15T12:01:00Z",
                        files=(),
                        databases=(main_database_record(stored.reference),),
                        schema_versions={"main": 14},
                        snapshot_id=first_snapshot_id,
                    )

            self.assertNotEqual(first.snapshot_dir, second.snapshot_dir)
            self.assertEqual(tree_file_bytes(first.snapshot_dir), first_before)
            self.assertTrue((collision_staging / "snapshot" / "commit.json").is_file())

    def test_degraded_snapshot_assigns_safe_generated_name_to_recovery_only_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repository"
            repository.mkdir()
            collection = root / "collection"
            collection.mkdir()
            collection_id = str(uuid.uuid4())
            repository_id = str(uuid.uuid4())
            source = root / "unsafe.bin"
            source.write_bytes(b"bevar")

            with RepositoryLock(repository, command="snapshot create"):
                initialize_repository(repository, collection, collection_id, repository_id=repository_id)
                staging = create_staging_run(repository)
                stored = store_verified_file(repository, staging, source)
                published = publish_snapshot(
                    repository,
                    staging,
                    collection_id=collection_id,
                    repository_id=repository_id,
                    status="degraded",
                    collection_identity_source="database",
                    started_at="2026-07-15T12:00:00Z",
                    completed_at="2026-07-15T12:01:00Z",
                    files=(
                        SnapshotFileRecord(
                            path=None,
                            original_path_display="CON.jpg",
                            restore_kind="recovery_only",
                            integrity_status="unsafe_path",
                            object=stored.reference,
                            mtime_ns=stored.source_mtime_ns,
                        ),
                    ),
                    databases=(main_database_record(stored.reference),),
                    schema_versions={"main": 14},
                )

            entry = json.loads(published.snapshot_dir.joinpath("files.jsonl").read_text(encoding="utf-8"))
            self.assertIsNone(entry["path"])
            self.assertEqual(entry["entry_id"], "e-000000000001")
            self.assertEqual(entry["recovery_name"], "entry-000000000001.bin")


def read_collection_id(target: Path) -> str:
    connection = sqlite3.connect(target / ".bilder.sqlite3")
    try:
        row = connection.execute("SELECT value FROM meta WHERE key = 'collection_id'").fetchone()
        assert row is not None
        return str(row[0])
    finally:
        connection.close()


def initialized_staging(root: Path) -> tuple[Path, str, str, Path]:
    repository = root / "repository"
    repository.mkdir()
    collection = root / "collection"
    collection.mkdir()
    collection_id = str(uuid.uuid4())
    repository_id = str(uuid.uuid4())
    with RepositoryLock(repository, command="snapshot create"):
        initialize_repository(repository, collection, collection_id, repository_id=repository_id)
        staging = create_staging_run(repository)
    return repository, collection_id, repository_id, staging


def normal_file_record(path: str, reference: ObjectReference) -> SnapshotFileRecord:
    return SnapshotFileRecord(
        path=path,
        original_path_display=path,
        restore_kind="normal",
        integrity_status="ok",
        object=reference,
        mtime_ns=0,
    )


def main_database_record(reference: ObjectReference) -> SnapshotDatabaseRecord:
    return SnapshotDatabaseRecord(
        role="main",
        source_path_display=".bilder.sqlite3",
        restore_path=".bilder.sqlite3",
        required=True,
        regenerable=False,
        capture="sqlite_backup",
        status="ok",
        object=reference,
        schema_version=14,
        model_name=None,
    )


def tree_file_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
