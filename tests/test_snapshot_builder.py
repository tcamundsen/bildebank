from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bildebank import snapshot_builder as snapshot_builder_module
from bildebank.config import FaceRecognitionConfig
from bildebank.snapshot_builder import SnapshotBuildResult, build_normal_snapshot
from bildebank.snapshot_repository import (
    RepositoryLock,
    PublishedSnapshot,
    SnapshotStorageError,
    SourceFileChangedError,
    SourceFileUnreadableError,
    create_staging_run,
    initialize_repository,
    publish_snapshot,
)
from bildebank.target_lock import TargetLock
from tests.cli_helpers import run_cli


class SnapshotBuilderTests(unittest.TestCase):
    def test_build_complete_snapshot_includes_active_deleted_unknown_and_provenance_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "collection"
            repository = root / "repository"
            repository.mkdir()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            content = b"samme bilde"
            active_path = "2026/07/active.jpg"
            deleted_path = "deleted/2025/12/deleted.jpg"
            write_file(target, active_path, content)
            write_file(target, deleted_path, content)
            sha256 = hashlib.sha256(content).hexdigest()
            active_id = insert_database_file(target, active_path, sha256, len(content))
            insert_database_file(target, deleted_path, sha256, len(content), deleted=True)
            insert_file_source(target, active_id)
            write_file(target, "notes/family.txt", b"bevar notatet")
            write_file(target, "thumbs/2026/07/active.jpg", b"thumbnail")

            result, published = build_and_publish(target, repository)

            self.assertEqual(result.status, "complete")
            self.assertEqual(len(result.files), 3)
            self.assertEqual(
                {record.original_path_display for record in result.files},
                {active_path, deleted_path, "notes/family.txt"},
            )
            media_objects = {
                record.object.sha256
                for record in result.files
                if record.original_path_display in {active_path, deleted_path} and record.object is not None
            }
            self.assertEqual(media_objects, {sha256})
            self.assertTrue(any("Ukjent fil" in warning for warning in result.warnings))
            self.assertTrue(any("thumbnails" in exclusion for exclusion in result.exclusions))
            manifest = json.loads(published.snapshot_dir.joinpath("manifest.json").read_bytes())
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual([database["role"] for database in manifest["databases"]], ["main"])

            main_object = result.databases[0].object
            assert main_object is not None
            main_path = object_path(repository, main_object.sha256, main_object.size_bytes)
            copied = sqlite3.connect(main_path)
            try:
                provenance_count = copied.execute("SELECT COUNT(*) FROM file_sources").fetchone()
            finally:
                copied.close()
            self.assertEqual(provenance_count, (1,))

    def test_build_degraded_snapshot_preserves_observed_variants_and_missing_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "collection"
            repository = root / "repository"
            repository.mkdir()
            self.assertEqual(run_cli(["create", str(target)]), 0)

            same_size_path = "2026/07/hash.jpg"
            expected_same_size = b"original"
            observed_same_size = b"changed!"
            self.assertEqual(len(expected_same_size), len(observed_same_size))
            write_file(target, same_size_path, observed_same_size)
            insert_database_file(
                target,
                same_size_path,
                hashlib.sha256(expected_same_size).hexdigest(),
                len(expected_same_size),
            )

            wrong_size_path = "2026/07/size.jpg"
            write_file(target, wrong_size_path, b"mye lengre innhold")
            insert_database_file(target, wrong_size_path, hashlib.sha256(b"kort").hexdigest(), len(b"kort"))
            missing_path = "2026/07/missing.jpg"
            insert_database_file(target, missing_path, hashlib.sha256(b"mangler").hexdigest(), len(b"mangler"))

            result, published = build_and_publish(target, repository)

            statuses = {record.original_path_display: record.integrity_status for record in result.files}
            self.assertEqual(result.status, "degraded")
            self.assertEqual(statuses[same_size_path], "hash_mismatch")
            self.assertEqual(statuses[wrong_size_path], "size_mismatch")
            self.assertEqual(statuses[missing_path], "missing")
            records = {record.original_path_display: record for record in result.files}
            self.assertIsNotNone(records[same_size_path].object)
            self.assertIsNotNone(records[wrong_size_path].object)
            self.assertIsNone(records[missing_path].object)
            observed_reference = records[same_size_path].object
            assert observed_reference is not None
            self.assertEqual(observed_reference.sha256, hashlib.sha256(observed_same_size).hexdigest())
            manifest = json.loads(published.snapshot_dir.joinpath("manifest.json").read_bytes())
            self.assertEqual(manifest["status"], "degraded")

    def test_database_catalog_captures_openclip_face_and_auxiliary_databases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "collection"
            repository = root / "repository"
            repository.mkdir()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            create_sqlite_database(target / ".bilder-openclip.sqlite3", schema_version=3)
            create_sqlite_database(target / ".bildebank-faces" / "antelopev2.sqlite3", schema_version=5)
            create_sqlite_database(target / "metadata" / "extra.sqlite3", schema_version=9)
            face_config = FaceRecognitionConfig(database_dir=Path(".bildebank-faces"))

            result, _published = build_and_publish(target, repository, face_config=face_config)

            self.assertEqual(
                {database.role for database in result.databases},
                {
                    "main",
                    "openclip",
                    "face:antelopev2",
                    "auxiliary:metadata/extra.sqlite3",
                },
            )
            self.assertEqual(result.schema_versions["main"], 14)
            self.assertEqual(result.schema_versions["openclip"], 3)
            self.assertEqual(result.schema_versions["face:antelopev2"], 5)
            self.assertTrue(any("Ukjent SQLite-database" in warning for warning in result.warnings))

    def test_unknown_file_is_retried_once_after_a_transient_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "collection"
            repository = root / "repository"
            repository.mkdir()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            unknown = target / "notes.txt"
            unknown.write_text("bevar", encoding="utf-8")
            original_store = snapshot_builder_module.store_verified_file
            attempts = 0

            def flaky_store(repository_path: Path, staging_path: Path, source_path: Path):  # noqa: ANN202
                nonlocal attempts
                if source_path == unknown:
                    attempts += 1
                    if attempts == 1:
                        raise SourceFileChangedError("midlertidig endring")
                return original_store(repository_path, staging_path, source_path)

            with patch("bildebank.snapshot_builder.store_verified_file", side_effect=flaky_store):
                result, _published = build_and_publish(target, repository)

            self.assertEqual(attempts, 2)
            self.assertEqual(result.status, "complete")
            record = next(record for record in result.files if record.original_path_display == "notes.txt")
            self.assertEqual(record.integrity_status, "ok")
            self.assertIsNotNone(record.object)

    def test_unknown_file_that_fails_twice_is_published_as_degraded_without_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "collection"
            repository = root / "repository"
            repository.mkdir()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            unknown = target / "notes.txt"
            unknown.write_text("bevar", encoding="utf-8")
            original_store = snapshot_builder_module.store_verified_file
            attempts = 0

            def unreadable_store(repository_path: Path, staging_path: Path, source_path: Path):  # noqa: ANN202
                nonlocal attempts
                if source_path == unknown:
                    attempts += 1
                    raise SourceFileUnreadableError("kan ikke leses")
                return original_store(repository_path, staging_path, source_path)

            with patch("bildebank.snapshot_builder.store_verified_file", side_effect=unreadable_store):
                result, published = build_and_publish(target, repository)

            self.assertEqual(attempts, 2)
            self.assertEqual(result.status, "degraded")
            record = next(record for record in result.files if record.original_path_display == "notes.txt")
            self.assertEqual(record.integrity_status, "unreadable")
            self.assertIsNone(record.object)
            manifest = json.loads(published.snapshot_dir.joinpath("manifest.json").read_bytes())
            self.assertEqual(manifest["status"], "degraded")

    def test_absolute_face_database_parent_of_collection_is_rejected_before_object_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "collection"
            repository = root / "repository"
            repository.mkdir()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            face_config = FaceRecognitionConfig(database_dir=root)

            with RepositoryLock(repository, command="snapshot create"):
                with TargetLock(target, command="snapshot create"):
                    collection_id = read_collection_id(target)
                    initialize_repository(repository, target, collection_id)
                    staging = create_staging_run(repository)
                    with self.assertRaisesRegex(SnapshotStorageError, "ligge i hverandre"):
                        build_normal_snapshot(
                            target,
                            repository,
                            staging,
                            face_config=face_config,
                        )

            object_files = [path for path in (repository / "objects").rglob("*") if path.is_file()]
            self.assertEqual(object_files, [])


def build_and_publish(
    target: Path,
    repository: Path,
    *,
    face_config: FaceRecognitionConfig | None = None,
) -> tuple[SnapshotBuildResult, PublishedSnapshot]:
    with RepositoryLock(repository, command="snapshot create"):
        with TargetLock(target, command="snapshot create"):
            collection_id = read_collection_id(target)
            metadata = initialize_repository(repository, target, collection_id)
            staging = create_staging_run(repository)
            result = build_normal_snapshot(
                target,
                repository,
                staging,
                face_config=face_config,
            )
            published = publish_snapshot(
                repository,
                staging,
                collection_id=result.collection_id,
                repository_id=result.repository_id,
                status=result.status,
                collection_identity_source="database",
                started_at="2026-07-15T12:00:00Z",
                completed_at="2026-07-15T12:01:00Z",
                files=result.files,
                databases=result.databases,
                schema_versions=result.schema_versions,
                exclusions=result.exclusions,
                warnings=result.warnings,
            )
            assert metadata["repository_id"] == result.repository_id
            return result, published


def insert_database_file(
    target: Path,
    relative_path: str,
    sha256: str,
    size_bytes: int,
    *,
    deleted: bool = False,
) -> int:
    connection = sqlite3.connect(target / ".bilder.sqlite3")
    try:
        cursor = connection.execute(
            """
            INSERT INTO files(
                target_path, target_path_key, original_filename, stored_filename,
                sha256, size_bytes, taken_date, date_source, name_conflict,
                deleted_at, deleted_original_target_path
            )
            VALUES(?, ?, ?, ?, ?, ?, '2026-07-15', 'filename', 0, ?, ?)
            """,
            (
                relative_path,
                relative_path.casefold(),
                Path(relative_path).name,
                Path(relative_path).name,
                sha256,
                size_bytes,
                "2026-07-15 12:00:00" if deleted else None,
                relative_path.removeprefix("deleted/") if deleted else None,
            ),
        )
        connection.commit()
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)
    finally:
        connection.close()


def insert_file_source(target: Path, file_id: int) -> None:
    connection = sqlite3.connect(target / ".bilder.sqlite3")
    try:
        cursor = connection.execute(
            "INSERT INTO sources(path, path_key, name) VALUES('D:/Kamera', 'd:/kamera', 'Kamera')"
        )
        assert cursor.lastrowid is not None
        source_id = int(cursor.lastrowid)
        connection.execute(
            """
            INSERT INTO file_sources(
                file_id, source_id, source_path, source_path_key, sha256, size_bytes
            )
            SELECT id, ?, 'D:/Kamera/active.jpg', 'd:/kamera/active.jpg', sha256, size_bytes
            FROM files WHERE id = ?
            """,
            (source_id, file_id),
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


def write_file(target: Path, relative_path: str, content: bytes) -> None:
    path = target / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def read_collection_id(target: Path) -> str:
    connection = sqlite3.connect(target / ".bilder.sqlite3")
    try:
        row = connection.execute("SELECT value FROM meta WHERE key = 'collection_id'").fetchone()
        assert row is not None
        return str(row[0])
    finally:
        connection.close()


def object_path(repository: Path, sha256: str, size_bytes: int) -> Path:
    return repository / "objects" / "sha256" / sha256[:2] / sha256[2:4] / f"{sha256}-{size_bytes}"
