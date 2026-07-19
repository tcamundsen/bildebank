from __future__ import annotations

import hashlib
import json
import os
import platform
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from bildebank.db import DB_FILENAME
from bildebank.snapshot import (
    REPOSITORY_LOCK_FILENAME,
    REPOSITORY_METADATA_FILENAME,
    plan_snapshot,
    snapshot_object_path,
)
from bildebank.target_lock import LOCK_FILENAME
from tests.cli_helpers import capture_cli, run_cli


class SnapshotCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.program_root_tempdir = tempfile.TemporaryDirectory()
        self.program_root = Path(self.program_root_tempdir.name)
        self.program_root_patcher = patch("bildebank.cli.program_repo_root", return_value=self.program_root)
        self.program_root_patcher.start()

    def tearDown(self) -> None:
        self.program_root_patcher.stop()
        self.program_root_tempdir.cleanup()

    def test_snapshot_dry_run_plans_exact_repository_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "Familiebilder"
            repository = root / "Min-versjonerte-backup"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / "notater.txt").write_text("bevar meg\n", encoding="utf-8")
            program_database = self.program_root / ".bildebank-program.sqlite3"
            program_database_before = program_database.read_bytes()
            source_files_before = tree_file_bytes(target)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", "--dry-run", str(repository)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Snapshot dry-run: leser og kontrollerer hoveddatabasen", stdout)
            self.assertIn("Snapshot dry-run: filer funnet=", stdout)
            self.assertIn("Snapshot dry-run: filinventar=", stdout)
            self.assertIn("Snapshot dry-run: beregner plassbehov", stdout)
            self.assertIn("Snapshot dry-run", stdout)
            self.assertIn(f"Repository: {repository}", stdout)
            self.assertIn("Mangler; ville blitt opprettet og initialisert", stdout)
            self.assertIn("Ukjente filer: 1", stdout)
            self.assertIn("Dry-run: Ingen filer, metadata, mapper eller låser", stdout)
            self.assertIn("Dry-run beregner ikke SHA-256", stderr)
            self.assertFalse(repository.exists())
            self.assertFalse((target / LOCK_FILENAME).exists())
            self.assertEqual(tree_file_bytes(target), source_files_before)
            self.assertEqual(program_database.read_bytes(), program_database_before)

    def test_snapshot_dry_run_accepts_empty_repository_without_initializing_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            repository.mkdir()
            self.assertEqual(run_cli(["create", str(target)]), 0)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", str(repository), "--dry-run"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Finnes og er tom; ville blitt initialisert", stdout)
            self.assertEqual(list(repository.iterdir()), [])

    def test_snapshot_dry_run_rejects_nonempty_uninitialized_repository_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            repository.mkdir()
            unrelated = repository / "familie.txt"
            unrelated.write_text("ikke rør\n", encoding="utf-8")
            self.assertEqual(run_cli(["create", str(target)]), 0)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", "--dry-run", str(repository)]
            )

            self.assertEqual(code, 1)
            self.assertIn("Snapshot dry-run:", stdout)
            self.assertIn("ikke tom", stderr)
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "ikke rør\n")
            self.assertFalse((repository / REPOSITORY_METADATA_FILENAME).exists())

    def test_snapshot_create_initializes_repository_and_publishes_complete_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            program_database = self.program_root / ".bildebank-program.sqlite3"
            program_database_before = program_database.read_bytes()

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "snapshot",
                    "create",
                    "--note",
                    "Første snapshot",
                    str(repository),
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Snapshot: lager filinventar", stdout)
            self.assertIn("Snapshot: publiserer manifest", stdout)
            self.assertIn("Snapshot opprettet", stdout)
            self.assertIn("Status: complete", stdout)
            self.assertIn("Repositoryet ble initialisert", stdout)
            self.assertEqual(stderr, "")
            self.assertTrue((repository / REPOSITORY_METADATA_FILENAME).is_file())
            snapshots = list((repository / "snapshots").iterdir())
            self.assertEqual(len(snapshots), 1)
            manifest = json.loads((snapshots[0] / "manifest.json").read_bytes())
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["note"], "Første snapshot")
            self.assertEqual(manifest["collection_identity"], {"source": "database", "verified": True})
            self.assertEqual(list((repository / "incomplete").iterdir()), [])
            self.assertFalse((target / LOCK_FILENAME).exists())
            self.assertFalse((repository / REPOSITORY_LOCK_FILENAME).exists())
            self.assertEqual(program_database.read_bytes(), program_database_before)

    def test_snapshot_create_appends_second_snapshot_without_changing_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)

            first_code, _stdout, first_stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", str(repository)]
            )
            self.assertEqual(first_code, 0, first_stderr)
            first_snapshot = next((repository / "snapshots").iterdir())
            first_snapshot_before = tree_file_bytes(first_snapshot)

            second_code, _stdout, second_stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", str(repository)]
            )
            check_code, check_stdout, check_stderr = capture_cli(
                ["snapshot", "check", str(repository)]
            )

            self.assertEqual(second_code, 0, second_stderr)
            self.assertEqual(len(list((repository / "snapshots").iterdir())), 2)
            self.assertEqual(tree_file_bytes(first_snapshot), first_snapshot_before)
            self.assertEqual(check_code, 0, check_stderr)
            self.assertIn("Publiserte, lesbare snapshots: 2", check_stdout)
            self.assertIn("Repositoryavvik: 0", check_stdout)

    def test_third_snapshot_without_source_changes_reuses_all_objects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / "første-notat.txt").write_text("første\n", encoding="utf-8")

            first_code, _stdout, first_stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", str(repository)]
            )
            self.assertEqual(first_code, 0, first_stderr)

            (target / "nytt-notat.txt").write_text("nytt i snapshot nummer to\n", encoding="utf-8")
            second_code, _stdout, second_stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", str(repository)]
            )
            self.assertEqual(second_code, 0, second_stderr)
            objects_before_third = tree_file_bytes(repository / "objects")

            third_code, _stdout, third_stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", str(repository)]
            )

            self.assertEqual(third_code, 0, third_stderr)
            self.assertEqual(len(list((repository / "snapshots").iterdir())), 3)
            self.assertGreater(len(objects_before_third), 0)
            self.assertEqual(tree_file_bytes(repository / "objects"), objects_before_third)

            check_code, check_stdout, check_stderr = capture_cli(
                ["snapshot", "check", str(repository)]
            )
            self.assertEqual(check_code, 0, check_stderr)
            self.assertIn("Publiserte, lesbare snapshots: 3", check_stdout)
            self.assertIn("Urefererte objekter: 0", check_stdout)
            self.assertIn("Repositoryavvik: 0", check_stdout)

    def test_snapshot_list_and_check_do_not_require_active_collection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            moved_target = root / "flyttet-target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / "notater.txt").write_text("familienotat\n", encoding="utf-8")
            create_code, _stdout, create_stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "snapshot",
                    "create",
                    "--note",
                    "Før flytting",
                    str(repository),
                ]
            )
            self.assertEqual(create_code, 0, create_stderr)
            target.rename(moved_target)

            list_code, list_stdout, list_stderr = capture_cli(
                ["snapshot", "list", str(repository)]
            )
            check_code, check_stdout, check_stderr = capture_cli(
                ["snapshot", "check", str(repository)]
            )

            self.assertEqual(list_code, 0, list_stderr)
            self.assertIn("Publiserte snapshots: 1", list_stdout)
            self.assertIn("Status: complete", list_stdout)
            self.assertIn("Kommentar: Før flytting", list_stdout)
            self.assertEqual(check_code, 0, check_stderr)
            self.assertIn("Rask snapshotkontroll fullført", check_stdout)
            self.assertIn("Repositoryavvik: 0", check_stdout)

    def test_snapshot_full_check_reports_same_size_corruption_and_affected_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / "notater.txt").write_text("familienotat\n", encoding="utf-8")
            create_code, _stdout, create_stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", str(repository)]
            )
            self.assertEqual(create_code, 0, create_stderr)
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

            quick_code, _quick_stdout, quick_stderr = capture_cli(
                ["snapshot", "check", str(repository)]
            )
            full_code, full_stdout, full_stderr = capture_cli(
                ["snapshot", "check", str(repository), "--full"]
            )

            self.assertEqual(quick_code, 0, quick_stderr)
            self.assertEqual(full_code, 3)
            self.assertIn("Full snapshotkontroll fullført", full_stdout)
            self.assertIn("Repositoryavvik: 1", full_stdout)
            self.assertIn("feil SHA-256", full_stderr)
            self.assertIn("sti=notater.txt", full_stderr)
            self.assertFalse((repository / REPOSITORY_LOCK_FILENAME).exists())

    def test_snapshot_create_returns_degraded_exit_code_for_media_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            expected = b"original"
            observed = b"changed!"
            self.assertEqual(len(expected), len(observed))
            relative_path = "2026/07/familie.jpg"
            media_path = target / relative_path
            media_path.parent.mkdir(parents=True)
            media_path.write_bytes(observed)
            insert_database_file(
                target,
                relative_path,
                hashlib.sha256(expected).hexdigest(),
                len(expected),
            )

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", str(repository)]
            )

            self.assertEqual(code, 3)
            self.assertIn("Status: degraded", stdout)
            self.assertIn("hash_mismatch", stderr)
            snapshot = next((repository / "snapshots").iterdir())
            manifest = json.loads((snapshot / "manifest.json").read_bytes())
            self.assertEqual(manifest["status"], "degraded")

    def test_snapshot_create_publishes_recovery_only_for_previously_bound_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            first_code, _stdout, first_stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", str(repository)]
            )
            self.assertEqual(first_code, 0, first_stderr)
            (target / DB_FILENAME).write_bytes(b"skadet hoveddatabase")

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", str(repository)]
            )

            self.assertEqual(code, 4)
            self.assertIn("Status: recovery", stdout)
            self.assertIn("Hoveddatabasen", stderr)
            snapshots = list((repository / "snapshots").iterdir())
            self.assertEqual(len(snapshots), 2)
            manifests = [json.loads((snapshot / "manifest.json").read_bytes()) for snapshot in snapshots]
            recovery_manifest = next(manifest for manifest in manifests if manifest["status"] == "recovery")
            self.assertEqual(recovery_manifest["status"], "recovery")
            self.assertEqual(
                recovery_manifest["collection_identity"],
                {"source": "repository", "verified": False},
            )

    def test_snapshot_create_refuses_recovery_with_new_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / DB_FILENAME).write_bytes(b"skadet hoveddatabase")

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", str(repository)]
            )

            self.assertEqual(code, 1)
            self.assertIn("Snapshot: lager filinventar", stdout)
            self.assertIn("Recovery krever et allerede initialisert repository", stderr)
            self.assertTrue(repository.is_dir())
            self.assertEqual(list(repository.iterdir()), [])
            self.assertFalse((target / LOCK_FILENAME).exists())

    def test_snapshot_create_takes_both_locks_before_initializing_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            target_lock = target / LOCK_FILENAME
            target_lock.write_text("command=import\npid=123\n", encoding="utf-8")

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", str(repository)]
            )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("låst av en annen bildebank-kommando", stderr)
            self.assertTrue(repository.is_dir())
            self.assertEqual(list(repository.iterdir()), [])
            self.assertEqual(target_lock.read_text(encoding="utf-8"), "command=import\npid=123\n")

    def test_snapshot_create_rejects_same_size_corrupt_existing_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            content = b"familiebilde"
            relative_path = "2026/07/familie.jpg"
            media_path = target / relative_path
            media_path.parent.mkdir(parents=True)
            media_path.write_bytes(content)
            sha256 = hashlib.sha256(content).hexdigest()
            insert_database_file(target, relative_path, sha256, len(content))
            first_code, _stdout, first_stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", str(repository)]
            )
            self.assertEqual(first_code, 0, first_stderr)
            object_path = snapshot_object_path(repository, sha256, len(content))
            corrupt_content = b"x" * len(content)
            object_path.write_bytes(corrupt_content)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", str(repository)]
            )

            self.assertEqual(code, 1)
            self.assertIn("Snapshot: byte=", stdout)
            self.assertIn("Eksisterende backupobjekt besto ikke SHA-256-kontroll", stderr)
            self.assertEqual(object_path.read_bytes(), corrupt_content)
            self.assertEqual(len(list((repository / "snapshots").iterdir())), 1)

    def test_snapshot_create_publish_failure_keeps_staging_and_releases_locks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)

            with patch("bildebank.snapshot_repository.os.rename", side_effect=OSError("avbrutt")):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "snapshot", "create", str(repository)]
                )

            self.assertEqual(code, 1)
            self.assertIn("Snapshot: publiserer manifest", stdout)
            self.assertIn("Kunne ikke publisere snapshotet atomisk", stderr)
            self.assertEqual(list((repository / "snapshots").iterdir()), [])
            incomplete_runs = list((repository / "incomplete").iterdir())
            self.assertEqual(len(incomplete_runs), 1)
            self.assertTrue((incomplete_runs[0] / "snapshot" / "commit.json").is_file())
            self.assertFalse((target / LOCK_FILENAME).exists())
            self.assertFalse((repository / REPOSITORY_LOCK_FILENAME).exists())

    def test_snapshot_dry_run_compares_database_files_and_reuses_existing_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            relative_path = "deleted/2024/01/familie.jpg"
            content = b"familiebilde"
            media_path = target / relative_path
            media_path.parent.mkdir(parents=True)
            media_path.write_bytes(content)
            sha256 = hashlib.sha256(content).hexdigest()
            insert_database_file(target, relative_path, sha256, len(content), deleted=True)
            collection_id = read_collection_id(target)
            write_repository_metadata(repository, target, collection_id)
            object_path = snapshot_object_path(repository, sha256, len(content))
            object_path.parent.mkdir(parents=True)
            object_path.write_bytes(content)

            plan = plan_snapshot(target, repository)

            self.assertEqual(plan.repository_state, "existing")
            self.assertEqual(plan.inventory.database_files, 1)
            self.assertEqual(plan.inventory.matched_database_files, 1)
            self.assertEqual(plan.inventory.missing_database_files, 0)
            self.assertEqual(plan.inventory.wrong_size_database_files, 0)
            self.assertEqual(plan.storage.reusable_objects, 1)

    def test_snapshot_dry_run_rejects_existing_object_with_wrong_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            relative_path = "2024/01/familie.jpg"
            content = b"familiebilde"
            media_path = target / relative_path
            media_path.parent.mkdir(parents=True)
            media_path.write_bytes(content)
            sha256 = hashlib.sha256(content).hexdigest()
            insert_database_file(target, relative_path, sha256, len(content))
            write_repository_metadata(repository, target, read_collection_id(target))
            object_path = snapshot_object_path(repository, sha256, len(content))
            object_path.parent.mkdir(parents=True)
            object_path.write_bytes(b"feil")

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", "--dry-run", str(repository)]
            )

            self.assertEqual(code, 1)
            self.assertIn("Snapshot dry-run:", stdout)
            self.assertIn("backupobjekt har feil størrelse", stderr)
            self.assertEqual(object_path.read_bytes(), b"feil")

    def test_snapshot_dry_run_rejects_repository_for_other_collection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            write_repository_metadata(repository, target, str(uuid.uuid4()))

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", "--dry-run", str(repository)]
            )

            self.assertEqual(code, 1)
            self.assertIn("Snapshot dry-run:", stdout)
            self.assertIn("annen bildesamling", stderr)

    def test_snapshot_dry_run_rejects_unknown_repository_root_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            write_repository_metadata(repository, target, read_collection_id(target))
            unrelated = repository / "ukjent.txt"
            unrelated.write_text("ikke rør\n", encoding="utf-8")

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", "--dry-run", str(repository)]
            )

            self.assertEqual(code, 1)
            self.assertIn("Snapshot dry-run:", stdout)
            self.assertIn("ukjent fil eller mappe", stderr)
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "ikke rør\n")

    def test_snapshot_dry_run_keeps_target_lock_and_excludes_generated_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            lock_path = target / LOCK_FILENAME
            lock_path.write_text("command=import\npid=123\n", encoding="utf-8")
            (target / ".bildebank.log").write_text("aktiv logg\n", encoding="utf-8")
            (target / "index.html").write_text("generert\n", encoding="utf-8")
            (target / "image-search.html").write_text("generert\n", encoding="utf-8")
            (target / "personer.html").write_text("generert\n", encoding="utf-8")
            (target / "person-Ola.html").write_text("generert\n", encoding="utf-8")
            thumbnail = target / "thumbs" / "2024" / "01" / "bilde.jpg"
            thumbnail.parent.mkdir(parents=True)
            thumbnail.write_bytes(b"thumb")

            plan = plan_snapshot(target, repository)

            self.assertEqual(plan.inventory.excluded_files, 7)
            self.assertEqual(
                {item.reason: item.files for item in plan.inventory.exclusions},
                {"thumbnails": 1, "generated_html": 4, "runtime": 2},
            )
            self.assertTrue(lock_path.exists())
            self.assertFalse(repository.exists())

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", "--dry-run", str(repository)]
            )
            self.assertEqual(code, 0, stderr)
            self.assertIn("thumbnails: 1", stdout)
            self.assertIn("generert HTML: 4", stdout)
            self.assertIn("runtime-filer: 2", stdout)

    def test_snapshot_dry_run_only_excludes_generated_names_at_standard_locations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            nested = target / "egne-filer"
            nested.mkdir()
            (nested / "index.html").write_text("bevar\n", encoding="utf-8")
            nested_thumbs = nested / "thumbs"
            nested_thumbs.mkdir()
            (nested_thumbs / "original.jpg").write_bytes(b"original")

            plan = plan_snapshot(target, repository)

            self.assertEqual(plan.inventory.excluded_files, 0)
            self.assertEqual(plan.inventory.unknown_files, 2)

    def test_snapshot_dry_run_keeps_orphaned_sqlite_side_file_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            orphaned_side_file = target / "tilfeldig.sqlite3-wal"
            orphaned_side_file.write_bytes(b"bevar meg")

            plan = plan_snapshot(target, repository)

            self.assertEqual(plan.inventory.database_side_files, 0)
            self.assertEqual(plan.inventory.unknown_files, 1)
            self.assertEqual(plan.inventory.unknown_bytes, len(b"bevar meg"))

    def test_snapshot_dry_run_recognizes_bildebank_migration_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            backup = target / f"{DB_FILENAME}.backup-before-schema-15-20260718-210818"
            backup.write_bytes(b"bevar meg")

            plan = plan_snapshot(target, repository)

            self.assertEqual(plan.inventory.migration_backup_files, 1)
            self.assertEqual(plan.inventory.migration_backup_bytes, len(b"bevar meg"))
            self.assertEqual(plan.inventory.unknown_files, 0)
            self.assertEqual(plan.storage.estimated_new_objects, 2)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", "--dry-run", str(repository)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Bildebank-migreringsbackuper: 1", stdout)

    def test_snapshot_dry_run_keeps_similarly_named_file_as_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / f"{DB_FILENAME}.backup-before-schema-not-a-version").write_bytes(b"bevar meg")

            plan = plan_snapshot(target, repository)

            self.assertEqual(plan.inventory.migration_backup_files, 0)
            self.assertEqual(plan.inventory.unknown_files, 1)

    def test_snapshot_dry_run_does_not_double_count_database_tracked_migration_like_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            relative_path = f"{DB_FILENAME}.backup-before-schema-15-20260718-210818"
            content = b"databasefort innhold"
            (target / relative_path).write_bytes(content)
            connection = sqlite3.connect(target / DB_FILENAME)
            try:
                connection.execute(
                    """
                    INSERT INTO files(
                        target_path, target_path_key, original_filename, stored_filename,
                        sha256, size_bytes, taken_date, date_source, name_conflict
                    )
                    VALUES(?, ?, ?, ?, ?, ?, '2026-07-18', 'filename', 0)
                    """,
                    (
                        relative_path,
                        relative_path.casefold(),
                        relative_path,
                        relative_path,
                        hashlib.sha256(content).hexdigest(),
                        len(content),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            plan = plan_snapshot(target, repository)

            self.assertEqual(plan.inventory.migration_backup_files, 0)
            self.assertEqual(plan.inventory.matched_database_files, 1)
            self.assertEqual(plan.storage.estimated_new_objects, 2)

    def test_snapshot_dry_run_classifies_side_file_for_existing_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            database = target / "ekstra.sqlite3"
            database.write_bytes(b"database")
            (target / "ekstra.sqlite3-wal").write_bytes(b"wal")

            plan = plan_snapshot(target, repository)

            self.assertEqual(plan.inventory.database_storage_files, 2)
            self.assertEqual(plan.inventory.database_side_files, 1)
            self.assertEqual(plan.inventory.unknown_files, 0)

    def test_snapshot_dry_run_rejects_symlink_without_following_it(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("Plattformen støtter ikke symbolske lenker")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            outside = root / "outside.txt"
            outside.write_text("utenfor\n", encoding="utf-8")
            self.assertEqual(run_cli(["create", str(target)]), 0)
            link = target / "lenke.txt"
            try:
                link.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"Kunne ikke opprette testlenke: {exc}")

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", "--dry-run", str(repository)]
            )

            self.assertEqual(code, 1)
            self.assertIn("Snapshot dry-run:", stdout)
            self.assertIn("Symbolske lenker", stderr)
            self.assertEqual(outside.read_text(encoding="utf-8"), "utenfor\n")
            self.assertFalse(repository.exists())

    @unittest.skipUnless(os.name == "nt", "Junction-test krever Windows")
    def test_snapshot_dry_run_rejects_windows_junction_before_repository_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            outside = root / "outside"
            outside.mkdir()
            marker = outside / "bevar.txt"
            marker.write_text("ikke følg junction\n", encoding="utf-8")
            self.assertEqual(run_cli(["create", str(target)]), 0)
            junction = target / "junction"
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if created.returncode != 0:
                self.skipTest(f"Kunne ikke opprette junction: {created.stderr or created.stdout}")

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", "--dry-run", str(repository)]
            )

            self.assertEqual(code, 1)
            self.assertIn("Snapshot dry-run:", stdout)
            self.assertIn("reparse", stderr.lower())
            self.assertEqual(marker.read_text(encoding="utf-8"), "ikke følg junction\n")
            self.assertFalse(repository.exists())

    @unittest.skipIf(os.name == "nt", "Windows tillater normalt ikke to slike filnavn")
    def test_snapshot_dry_run_marks_windows_case_collision_as_recovery_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / "Photo.jpg").write_bytes(b"one")
            (target / "photo.jpg").write_bytes(b"two")

            plan = plan_snapshot(target, repository)

            self.assertEqual(plan.inventory.path_collisions, 1)
            self.assertEqual(plan.inventory.recovery_only_files, 2)
            self.assertEqual(plan.inventory.unknown_files, 2)

    def test_snapshot_note_validation_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "snapshot",
                    "create",
                    "--dry-run",
                    "--note",
                    "linje 1\nlinje 2",
                    str(repository),
                ]
            )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("kontrolltegn", stderr)
            self.assertFalse(repository.exists())

    def test_snapshot_dry_run_does_not_migrate_legacy_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            config_path = self.program_root / "bildebank-config.toml"
            config_path.write_text(
                "[openclip]\nenabled = false\nmodel_name = \"ViT-B-32\"\n",
                encoding="utf-8",
            )
            before = config_path.read_bytes()

            code, _stdout, stderr = capture_cli(
                ["--target", str(target), "snapshot", "create", "--dry-run", str(repository)]
            )

            self.assertEqual(code, 0, stderr)
            self.assertEqual(config_path.read_bytes(), before)
            self.assertFalse(repository.exists())

    def test_snapshot_dry_run_rejects_file_over_repository_file_size_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            repository = root / "repository"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            limit = (target / DB_FILENAME).stat().st_size + 16
            oversized = target / "for-stor.bin"
            oversized.write_bytes(b"x" * (limit + 1))

            with patch("bildebank.snapshot.repository_file_size_limit", return_value=limit):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "snapshot", "create", "--dry-run", str(repository)]
                )

            self.assertEqual(code, 1)
            self.assertIn("Snapshot dry-run:", stdout)
            self.assertIn("per-fil-grense", stderr)
            self.assertTrue(oversized.exists())
            self.assertFalse(repository.exists())


def insert_database_file(
    target: Path,
    relative_path: str,
    sha256: str,
    size_bytes: int,
    *,
    deleted: bool = False,
) -> None:
    conn = sqlite3.connect(target / DB_FILENAME)
    try:
        conn.execute(
            """
            INSERT INTO files(
                target_path, target_path_key, original_filename, stored_filename,
                sha256, size_bytes, taken_date, date_source, name_conflict,
                deleted_at, deleted_original_target_path
            )
            VALUES(?, ?, ?, ?, ?, ?, '2024-01-02', 'filename', 0, ?, ?)
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
        conn.commit()
    finally:
        conn.close()


def read_collection_id(target: Path) -> str:
    conn = sqlite3.connect(target / DB_FILENAME)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'collection_id'").fetchone()
        assert row is not None
        return str(row[0])
    finally:
        conn.close()


def write_repository_metadata(repository: Path, target: Path, collection_id: str) -> None:
    repository.mkdir()
    metadata = {
        "collection_id": collection_id,
        "collection_name": target.name,
        "created_at": "2026-07-15T12:00:00Z",
        "created_by": {"program": "bildebank", "version": "0.9.0"},
        "format_version": 1,
        "last_confirmed_source": {
            "collection_path": str(target.resolve()),
            "confirmed_at": "2026-07-15T12:00:00Z",
            "machine_name": platform.node() or "test-machine",
        },
        "repository_id": str(uuid.uuid4()),
        "required_features": [],
    }
    (repository / REPOSITORY_METADATA_FILENAME).write_text(
        json.dumps(metadata, ensure_ascii=False),
        encoding="utf-8",
    )


def tree_file_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
