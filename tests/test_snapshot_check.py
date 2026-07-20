from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from bildebank import db
from bildebank.snapshot import REPOSITORY_LOCK_FILENAME, snapshot_object_path
from bildebank.snapshot_check import check_snapshot_repository, list_repository_snapshots
from bildebank.snapshot_create import create_snapshot


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


def entry_snapshot_id(snapshot: Path) -> str:
    manifest = json.loads((snapshot / "manifest.json").read_bytes())
    return str(manifest["snapshot_id"])


def tree_file_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
