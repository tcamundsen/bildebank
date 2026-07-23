from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.snapshot import REPOSITORY_LOCK_FILENAME, snapshot_object_path
from bildebank.snapshot_check import (
    SnapshotCheckIssue,
    check_snapshot_repository,
    list_repository_snapshots,
    scan_object_store,
)
from bildebank.snapshot_create import create_snapshot
from bildebank.snapshot_repository import SnapshotStorageError, canonical_json_bytes
from bildebank.snapshot_restore import (
    plan_full_restore,
    restore_full_snapshot,
)


class SnapshotCheckTests(unittest.TestCase):
    def test_list_and_quick_check_read_published_snapshot_without_active_collection(self) -> None:
        with snapshot_repository() as (_root, target, repository):
            target.rename(target.with_name("samlingen-er-flyttet"))
            before = tree_file_bytes(repository)

            listed = list_repository_snapshots(repository)
            checked = check_snapshot_repository(repository)

            self.assertEqual(len(listed.snapshots), 1)
            self.assertEqual(listed.snapshots[0].status, "complete")
            self.assertEqual(checked.exit_code, 0)
            self.assertTrue(checked.completed)
            self.assertGreater(checked.referenced_objects, 0)
            self.assertEqual(tree_file_bytes(repository), before)
            self.assertFalse((repository / REPOSITORY_LOCK_FILENAME).exists())

    def test_full_check_hashes_referenced_and_unreferenced_objects(self) -> None:
        with snapshot_repository() as (_root, _target, repository):
            unreferenced_content = b"ferdig verifisert, men ureferert"
            sha256 = hashlib.sha256(unreferenced_content).hexdigest()
            unreferenced = snapshot_object_path(repository, sha256, len(unreferenced_content))
            unreferenced.parent.mkdir(parents=True, exist_ok=True)
            unreferenced.write_bytes(unreferenced_content)
            progress = []
            before = tree_file_bytes(repository)

            result = check_snapshot_repository(repository, full=True, progress=progress.append)

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.unreferenced_objects, 1)
            self.assertEqual(result.checked_objects, result.total_objects)
            self.assertEqual(result.checked_bytes, result.total_bytes)
            self.assertEqual(progress[-1].checked_objects, result.total_objects)
            self.assertEqual(tree_file_bytes(repository), before)

    def test_full_check_reports_corrupt_unreferenced_object_without_affected_snapshot(self) -> None:
        with snapshot_repository() as (_root, _target, repository):
            original = b"ureferert objekt"
            sha256 = hashlib.sha256(original).hexdigest()
            object_path = snapshot_object_path(repository, sha256, len(original))
            object_path.parent.mkdir(parents=True, exist_ok=True)
            object_path.write_bytes(b"x" * len(original))

            result = check_snapshot_repository(repository, full=True)

            self.assertEqual(result.exit_code, 3)
            issue = next(issue for issue in result.issues if issue.code == "object_hash_mismatch")
            self.assertEqual(issue.affected, ())
            self.assertEqual(result.unreferenced_objects, 1)

    def test_quick_check_finds_missing_object_and_all_affected_paths(self) -> None:
        with snapshot_repository() as (_root, target, repository):
            create_snapshot(target, repository, note="Andre snapshot")
            snapshot = next((repository / "snapshots").iterdir())
            entry = json.loads((snapshot / "files.jsonl").read_text(encoding="utf-8").splitlines()[0])
            reference = entry["object"]
            object_path = snapshot_object_path(
                repository,
                reference["sha256"],
                int(reference["size_bytes"]),
            )
            object_path.unlink()

            result = check_snapshot_repository(repository)

            self.assertEqual(result.exit_code, 3)
            issue = next(issue for issue in result.issues if issue.code == "missing_object")
            self.assertEqual(len(issue.affected), 2)
            self.assertIn(entry_snapshot_id(snapshot), {item.snapshot_id for item in issue.affected})
            self.assertEqual({item.logical_path for item in issue.affected}, {entry["path"]})

    def test_check_finds_database_file_missing_from_files_jsonl(self) -> None:
        with database_media_snapshot_repository() as (
            _root,
            _target,
            repository,
            snapshot,
            relative_path,
        ):
            remove_all_file_entries(snapshot)
            before = tree_file_bytes(repository)

            quick = check_snapshot_repository(repository)
            full = check_snapshot_repository(repository, full=True)

            for result in (quick, full):
                self.assertEqual(result.exit_code, 3)
                issue = next(
                    issue
                    for issue in result.issues
                    if issue.code == "database_file_mismatch"
                )
                self.assertIn(relative_path, issue.message)
                self.assertIn(entry_snapshot_id(snapshot), issue.message)
                self.assertEqual(len(result.snapshots), 1)
            self.assertEqual(tree_file_bytes(repository), before)

    def test_check_finds_expected_value_that_disagrees_with_database(self) -> None:
        with database_media_snapshot_repository() as (
            _root,
            _target,
            repository,
            snapshot,
            relative_path,
        ):
            entry = json.loads(
                (snapshot / "files.jsonl").read_text(encoding="utf-8")
            )
            entry["expected"]["sha256"] = "0" * 64
            entry["object"]["sha256"] = "0" * 64
            rewrite_file_entries(snapshot, (entry,))

            result = check_snapshot_repository(repository)

            self.assertEqual(result.exit_code, 3)
            issue = next(
                issue
                for issue in result.issues
                if issue.code == "database_file_mismatch"
            )
            self.assertIn("Expected-verdien", issue.message)
            self.assertIn(relative_path, issue.message)

    def test_full_restore_rejects_database_file_missing_from_files_jsonl(self) -> None:
        with database_media_snapshot_repository() as (
            root,
            _target,
            repository,
            snapshot,
            relative_path,
        ):
            remove_all_file_entries(snapshot)
            snapshot_id = entry_snapshot_id(snapshot)
            restored = root / "restored"
            before = tree_file_bytes(repository)

            with self.assertRaisesRegex(
                SnapshotStorageError,
                f"files-rad.*{relative_path}.*mangler filpost",
            ):
                plan_full_restore(repository, snapshot_id, restored)
            with self.assertRaisesRegex(
                SnapshotStorageError,
                f"files-rad.*{relative_path}.*mangler filpost",
            ):
                restore_full_snapshot(repository, snapshot_id, restored)

            self.assertFalse(restored.exists())
            self.assertEqual(list(root.glob(".bildebank-restore-restored-*")), [])
            self.assertEqual(tree_file_bytes(repository), before)

    def test_final_restore_validation_rejects_omitted_database_media_output(self) -> None:
        with database_media_snapshot_repository() as (
            root,
            _target,
            repository,
            snapshot,
            relative_path,
        ):
            restored = root / "restored"
            snapshot_id = entry_snapshot_id(snapshot)
            plan = plan_full_restore(repository, snapshot_id, restored)
            broken_plan = replace(
                plan,
                collection_outputs=tuple(
                    output
                    for output in plan.collection_outputs
                    if output.relative_path != relative_path
                ),
            )

            with (
                patch(
                    "bildebank.snapshot_restore.build_full_restore_plan",
                    return_value=broken_plan,
                ),
                self.assertRaisesRegex(
                    SnapshotStorageError,
                    f"Restoreutfallet mangler.*{relative_path}",
                ),
            ):
                restore_full_snapshot(repository, snapshot_id, restored)

            self.assertFalse(restored.exists())
            staging = next(root.glob(".bildebank-restore-restored-*"))
            self.assertTrue((staging / "collection" / db.DB_FILENAME).is_file())

    def test_full_check_finds_same_size_hash_corruption_and_affected_path(self) -> None:
        with snapshot_repository() as (_root, _target, repository):
            snapshot = next((repository / "snapshots").iterdir())
            entries = [
                json.loads(line)
                for line in (snapshot / "files.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            entry = next(item for item in entries if item["path"] == "notater.txt")
            reference = entry["object"]
            object_path = snapshot_object_path(
                repository,
                reference["sha256"],
                int(reference["size_bytes"]),
            )
            object_path.write_bytes(b"x" * object_path.stat().st_size)

            quick = check_snapshot_repository(repository)
            full = check_snapshot_repository(repository, full=True)

            self.assertEqual(quick.exit_code, 0)
            self.assertEqual(full.exit_code, 3)
            issue = next(issue for issue in full.issues if issue.code == "object_hash_mismatch")
            self.assertEqual(issue.affected[0].logical_path, "notater.txt")

    def test_check_reports_invalid_commit_and_nonempty_incomplete_run(self) -> None:
        with snapshot_repository() as (_root, _target, repository):
            snapshot = next((repository / "snapshots").iterdir())
            (snapshot / "commit.json").write_text("{}\n", encoding="utf-8")
            interrupted = repository / "incomplete" / "avbrutt-kjoring"
            interrupted.mkdir()
            (interrupted / "bevar.bin").write_bytes(b"skal ikke slettes")

            result = check_snapshot_repository(repository)

            self.assertEqual(result.exit_code, 3)
            self.assertTrue(any(issue.code == "invalid_snapshot" for issue in result.issues))
            self.assertEqual(result.incomplete_runs[0].run_id, "avbrutt-kjoring")
            self.assertEqual((interrupted / "bevar.bin").read_bytes(), b"skal ikke slettes")

    def test_check_reports_wrong_manifest_value_types_as_invalid_snapshot(self) -> None:
        cases = (
            ("format_version", True),
            ("status", []),
        )
        for field, invalid_value in cases:
            with self.subTest(field=field), snapshot_repository() as (
                _root,
                _target,
                repository,
            ):
                snapshot = next((repository / "snapshots").iterdir())
                manifest = json.loads((snapshot / "manifest.json").read_bytes())
                manifest[field] = invalid_value
                rewrite_manifest(snapshot, manifest)
                before = tree_file_bytes(repository)

                result = check_snapshot_repository(repository)

                self.assertEqual(result.exit_code, 3)
                self.assertEqual(result.snapshots, ())
                self.assertTrue(
                    any(issue.code == "invalid_snapshot" for issue in result.issues)
                )
                self.assertEqual(tree_file_bytes(repository), before)

    def test_check_reports_wrong_database_value_types_as_invalid_snapshot(self) -> None:
        cases = (
            ("role", 123),
            ("source_path_display", 123),
            ("restore_path", 123),
            ("capture", []),
            ("status", []),
        )
        for field, invalid_value in cases:
            with self.subTest(field=field), snapshot_repository() as (
                _root,
                _target,
                repository,
            ):
                snapshot = next((repository / "snapshots").iterdir())
                manifest = json.loads((snapshot / "manifest.json").read_bytes())
                manifest["databases"][0][field] = invalid_value
                rewrite_manifest(snapshot, manifest)
                before = tree_file_bytes(repository)

                result = check_snapshot_repository(repository)

                self.assertEqual(result.exit_code, 3)
                self.assertEqual(result.snapshots, ())
                self.assertTrue(
                    any(issue.code == "invalid_snapshot" for issue in result.issues)
                )
                self.assertEqual(tree_file_bytes(repository), before)

    def test_check_reports_wrong_file_value_types_as_invalid_snapshot(self) -> None:
        cases = (
            ("original_path_display", 123),
            ("path", 123),
            ("record_type", []),
            ("restore_kind", []),
            ("integrity_status", []),
        )
        for field, invalid_value in cases:
            with self.subTest(field=field), snapshot_repository() as (
                _root,
                _target,
                repository,
            ):
                snapshot = next((repository / "snapshots").iterdir())
                entries = tuple(
                    json.loads(line)
                    for line in (snapshot / "files.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                )
                entries[0][field] = invalid_value
                rewrite_file_entries(snapshot, entries)
                before = tree_file_bytes(repository)

                result = check_snapshot_repository(repository)

                self.assertEqual(result.exit_code, 3)
                self.assertEqual(result.snapshots, ())
                self.assertTrue(
                    any(issue.code == "invalid_snapshot" for issue in result.issues)
                )
                self.assertEqual(tree_file_bytes(repository), before)

    def test_check_rejects_boolean_commit_format_version(self) -> None:
        with snapshot_repository() as (_root, _target, repository):
            snapshot = next((repository / "snapshots").iterdir())
            commit = json.loads((snapshot / "commit.json").read_bytes())
            commit["format_version"] = True
            (snapshot / "commit.json").write_bytes(canonical_json_bytes(commit))
            before = tree_file_bytes(repository)

            result = check_snapshot_repository(repository)

            self.assertEqual(result.exit_code, 3)
            self.assertEqual(result.snapshots, ())
            self.assertTrue(
                any(issue.code == "invalid_snapshot" for issue in result.issues)
            )
            self.assertEqual(tree_file_bytes(repository), before)

    def test_check_rejects_snapshot_directory_marked_as_reparse_point(self) -> None:
        with snapshot_repository() as (_root, _target, repository):
            snapshot = next((repository / "snapshots").iterdir())
            snapshot_inode = snapshot.stat(follow_symlinks=False).st_ino
            before = tree_file_bytes(repository)

            with patch(
                "bildebank.snapshot_check.is_reparse_stat",
                side_effect=lambda path_stat: path_stat.st_ino == snapshot_inode,
            ):
                result = check_snapshot_repository(repository)

            self.assertEqual(result.exit_code, 3)
            self.assertEqual(result.snapshots, ())
            self.assertTrue(
                any(issue.code == "invalid_snapshot_entry" for issue in result.issues)
            )
            self.assertEqual(tree_file_bytes(repository), before)

    def test_object_scan_rejects_nested_directories_marked_as_reparse_points(self) -> None:
        for location in ("sha256-root", "prefix-directory"):
            with self.subTest(location=location), snapshot_repository() as (
                _root,
                _target,
                repository,
            ):
                object_root = repository / "objects" / "sha256"
                tagged = (
                    object_root
                    if location == "sha256-root"
                    else next(path for path in object_root.iterdir() if path.is_dir())
                )
                tagged_inode = tagged.stat(follow_symlinks=False).st_ino
                issues: list[SnapshotCheckIssue] = []

                with patch(
                    "bildebank.snapshot_check.is_reparse_stat",
                    side_effect=lambda path_stat: path_stat.st_ino == tagged_inode,
                ):
                    objects = scan_object_store(repository, issues)

                expected_code = (
                    "invalid_object_store"
                    if location == "sha256-root"
                    else "linked_object_entry"
                )
                self.assertTrue(any(issue.code == expected_code for issue in issues))
                self.assertFalse(
                    any(item.path.is_relative_to(tagged) for item in objects.values())
                )

    def test_incomplete_scan_rejects_directories_marked_as_reparse_points(self) -> None:
        for location in ("run", "nested"):
            with self.subTest(location=location), snapshot_repository() as (
                _root,
                _target,
                repository,
            ):
                run = repository / "incomplete" / "avbrutt"
                nested = run / "nested"
                nested.mkdir(parents=True)
                (nested / "utenfor.bin").write_bytes(b"skal ikke telles")
                tagged = run if location == "run" else nested
                tagged_inode = tagged.stat(follow_symlinks=False).st_ino

                with patch(
                    "bildebank.snapshot_check.is_reparse_stat",
                    side_effect=lambda path_stat: path_stat.st_ino == tagged_inode,
                ):
                    result = check_snapshot_repository(repository)

                expected_code = (
                    "invalid_incomplete_entry"
                    if location == "run"
                    else "unsafe_incomplete"
                )
                self.assertTrue(
                    any(issue.code == expected_code for issue in result.issues)
                )
                if location == "run":
                    self.assertEqual(result.incomplete_runs, ())
                else:
                    self.assertEqual(result.incomplete_runs[0].size_bytes, 0)

    @unittest.skipUnless(os.name == "nt", "Junction-test krever Windows")
    def test_check_rejects_nested_windows_junction_without_following_it(self) -> None:
        with snapshot_repository() as (root, _target, repository):
            outside = root / "outside"
            outside.mkdir()
            marker = outside / "bevar.txt"
            marker.write_text("ikke følg junction\n", encoding="utf-8")
            junction = repository / "incomplete" / "junction-run"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if created.returncode != 0:
                self.skipTest(
                    f"Kunne ikke opprette junction: {created.stderr or created.stdout}"
                )

            result = check_snapshot_repository(repository)

            self.assertEqual(result.exit_code, 3)
            self.assertTrue(
                any(
                    issue.code == "invalid_incomplete_entry"
                    for issue in result.issues
                )
            )
            self.assertEqual(marker.read_text(encoding="utf-8"), "ikke følg junction\n")
            self.assertEqual(result.incomplete_runs, ())

    def test_full_check_can_cancel_without_changing_repository(self) -> None:
        with snapshot_repository() as (_root, _target, repository):
            before = tree_file_bytes(repository)

            result = check_snapshot_repository(repository, full=True, should_cancel=lambda: True)

            self.assertFalse(result.completed)
            self.assertTrue(result.cancelled)
            self.assertEqual(result.exit_code, 1)
            self.assertEqual(result.checked_objects, 0)
            self.assertEqual(tree_file_bytes(repository), before)
            self.assertFalse((repository / REPOSITORY_LOCK_FILENAME).exists())

    def test_list_and_check_reject_existing_repository_lock_without_changing_it(self) -> None:
        with snapshot_repository() as (_root, _target, repository):
            lock_path = repository / REPOSITORY_LOCK_FILENAME
            lock_path.write_text("opptatt\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "låst"):
                list_repository_snapshots(repository)
            with self.assertRaisesRegex(ValueError, "låst"):
                check_snapshot_repository(repository, full=True)

            self.assertEqual(lock_path.read_text(encoding="utf-8"), "opptatt\n")


class snapshot_repository:
    def __init__(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()

    def __enter__(self) -> tuple[Path, Path, Path]:
        root = Path(self._tempdir.name)
        target = root / "collection"
        repository = root / "repository"
        db.init_database(target)
        (target / "notater.txt").write_text("familienotat\n", encoding="utf-8")
        create_snapshot(target, repository, note="Første snapshot")
        return root, target, repository

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self._tempdir.cleanup()


class database_media_snapshot_repository:
    def __init__(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()

    def __enter__(self) -> tuple[Path, Path, Path, Path, str]:
        root = Path(self._tempdir.name)
        target = root / "collection"
        repository = root / "repository"
        relative_path = "2026/07/familie.jpg"
        media_bytes = b"databasefort mediefil"
        db.init_database(target)
        media = target / relative_path
        media.parent.mkdir(parents=True)
        media.write_bytes(media_bytes)
        connection = sqlite3.connect(target / db.DB_FILENAME)
        try:
            connection.execute(
                """
                INSERT INTO files(
                    target_path, target_path_key, original_filename, stored_filename,
                    sha256, size_bytes, taken_date, date_source, name_conflict
                )
                VALUES(?, ?, 'familie.jpg', 'familie.jpg', ?, ?, '2026-07-15', 'filename', 0)
                """,
                (
                    relative_path,
                    relative_path.casefold(),
                    hashlib.sha256(media_bytes).hexdigest(),
                    len(media_bytes),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        result = create_snapshot(target, repository)
        return root, target, repository, result.published.snapshot_dir, relative_path

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self._tempdir.cleanup()


def remove_all_file_entries(snapshot: Path) -> None:
    rewrite_file_entries(snapshot, ())


def rewrite_manifest(snapshot: Path, manifest: dict[str, object]) -> None:
    manifest_content = canonical_json_bytes(manifest)
    (snapshot / "manifest.json").write_bytes(manifest_content)

    commit = json.loads((snapshot / "commit.json").read_bytes())
    commit["manifest"] = {
        "sha256": hashlib.sha256(manifest_content).hexdigest(),
        "size_bytes": str(len(manifest_content)),
    }
    (snapshot / "commit.json").write_bytes(canonical_json_bytes(commit))


def rewrite_file_entries(
    snapshot: Path,
    entries: tuple[dict[str, object], ...],
) -> None:
    files_content = b"".join(canonical_json_bytes(entry) for entry in entries)
    files_reference = {
        "sha256": hashlib.sha256(files_content).hexdigest(),
        "size_bytes": str(len(files_content)),
    }
    (snapshot / "files.jsonl").write_bytes(files_content)

    manifest = json.loads((snapshot / "manifest.json").read_bytes())
    manifest["files_jsonl"] = {
        "entry_count": str(len(entries)),
        **files_reference,
    }
    manifest_content = canonical_json_bytes(manifest)
    (snapshot / "manifest.json").write_bytes(manifest_content)

    commit = json.loads((snapshot / "commit.json").read_bytes())
    commit["files_jsonl"] = files_reference
    commit["manifest"] = {
        "sha256": hashlib.sha256(manifest_content).hexdigest(),
        "size_bytes": str(len(manifest_content)),
    }
    (snapshot / "commit.json").write_bytes(canonical_json_bytes(commit))


def entry_snapshot_id(snapshot: Path) -> str:
    manifest = json.loads((snapshot / "manifest.json").read_bytes())
    return str(manifest["snapshot_id"])


def tree_file_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
