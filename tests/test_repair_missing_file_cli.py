from __future__ import annotations

import errno
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.cli import main
from bildebank.missing_file_repair import publish_temporary_no_replace
from bildebank.target_lock import LOCK_FILENAME
from tests.cli_helpers import capture_cli
from tests.db_test_helpers import register_target_file


class RepairMissingFileCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.program_root_tempdir = tempfile.TemporaryDirectory()
        self.program_root = Path(self.program_root_tempdir.name)
        self.program_root_patcher = patch(
            "bildebank.cli.program_repo_root",
            return_value=self.program_root,
        )
        self.program_root_patcher.start()

    def tearDown(self) -> None:
        self.program_root_patcher.stop()
        self.program_root_tempdir.cleanup()

    def test_help_explains_explicit_copy_and_apply(self) -> None:
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            main(["repair-missing-file", "-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = output.getvalue()
        self.assertIn(
            "usage: bildebank repair-missing-file [valg] "
            "fil-id gjenopprettet-fil",
            stdout,
        )
        self.assertIn("fil-id", stdout)
        self.assertIn("gjenopprettet-fil", stdout)
        self.assertIn("--apply", stdout)

    def test_dry_run_verifies_copy_without_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target, candidate, destination, file_id = (
                create_missing_file_fixture(Path(tmp))
            )
            database_before = db.db_path_for_target(target).read_bytes()

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "repair-missing-file",
                    str(file_id),
                    str(candidate),
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn(f"Databasepost: file #{file_id} (aktiv)", stdout)
            self.assertIn("Mål i samlingen: 2024/01/missing.avi", stdout)
            self.assertIn("Dry-run: kopien stemmer eksakt.", stdout)
            self.assertIn("Ingen endringer er gjort.", stdout)
            self.assertIn("Ta et oppdatert snapshot", stdout)
            self.assertIn("--apply", stdout)
            self.assertFalse(destination.exists())
            self.assertEqual(candidate.read_bytes(), b"recovered-video")
            self.assertEqual(list(self.program_root.iterdir()), [])
            self.assertEqual(
                db.db_path_for_target(target).read_bytes(),
                database_before,
            )
            self.assertFalse((target / LOCK_FILENAME).exists())

    def test_apply_restores_active_file_and_keeps_external_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target, candidate, destination, file_id = (
                create_missing_file_fixture(Path(tmp))
            )
            destination.parent.rmdir()
            destination.parent.parent.rmdir()
            database_before = db.db_path_for_target(target).read_bytes()

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "repair-missing-file",
                    str(file_id),
                    str(candidate),
                    "--apply",
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn(f"Gjenopprettet fil: {destination}", stdout)
            self.assertIn("kopien utenfor samlingen er beholdt", stdout)
            self.assertEqual(destination.read_bytes(), b"recovered-video")
            self.assertEqual(candidate.read_bytes(), b"recovered-video")
            self.assertEqual(
                db.db_path_for_target(target).read_bytes(),
                database_before,
            )
            self.assertEqual(
                list(destination.parent.glob(".*.bildebank-repair-*.tmp")),
                [],
            )
            self.assertFalse((target / LOCK_FILENAME).exists())

    def test_apply_restores_deleted_file_under_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target, candidate, destination, file_id = (
                create_missing_file_fixture(Path(tmp), deleted=True)
            )

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "repair-missing-file",
                    str(file_id),
                    str(candidate),
                    "--apply",
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn(f"Databasepost: file #{file_id} (slettet)", stdout)
            self.assertEqual(destination.read_bytes(), b"recovered-video")
            self.assertTrue(candidate.exists())

    def test_refuses_changed_candidate_and_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target, candidate, destination, file_id = (
                create_missing_file_fixture(Path(tmp))
            )
            candidate.write_bytes(b"changed-content")

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "repair-missing-file",
                    str(file_id),
                    str(candidate),
                    "--apply",
                ]
            )

            self.assertEqual(code, 1, stdout)
            self.assertIn("ikke samme SHA-256", stderr)
            self.assertFalse(destination.exists())

            candidate.write_bytes(b"recovered-video")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"existing-file")
            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "repair-missing-file",
                    str(file_id),
                    str(candidate),
                    "--apply",
                ]
            )

            self.assertEqual(code, 1, stdout)
            self.assertIn("mangler ikke", stderr)
            self.assertIn("Ingen fil blir overskrevet", stderr)
            self.assertEqual(destination.read_bytes(), b"existing-file")
            self.assertEqual(candidate.read_bytes(), b"recovered-video")

    def test_refuses_missing_provenance_and_candidate_inside_collection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target, candidate, _destination, file_id = (
                create_missing_file_fixture(Path(tmp))
            )
            conn = db.connect(target)
            try:
                conn.execute(
                    "DELETE FROM file_sources WHERE file_id = ?",
                    (file_id,),
                )
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "repair-missing-file",
                    str(file_id),
                    str(candidate),
                ]
            )

            self.assertEqual(code, 1, stdout)
            self.assertIn("mangler file_sources-proveniens", stderr)

        with tempfile.TemporaryDirectory() as tmp:
            target, candidate, _destination, file_id = (
                create_missing_file_fixture(Path(tmp))
            )
            inside = target / "recovered-copy.avi"
            inside.write_bytes(candidate.read_bytes())

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "repair-missing-file",
                    str(file_id),
                    str(inside),
                ]
            )

            self.assertEqual(code, 1, stdout)
            self.assertIn("må ligge utenfor bildesamlingen", stderr)

    def test_refuses_pending_move_without_running_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target, candidate, destination, file_id = (
                create_missing_file_fixture(Path(tmp))
            )
            conn = db.connect(target)
            try:
                expected_sha256 = str(
                    conn.execute(
                        "SELECT sha256 FROM files WHERE id = ?",
                        (file_id,),
                    ).fetchone()["sha256"]
                )
                db.create_pending_file_move(
                    conn,
                    file_id=file_id,
                    target_root=target,
                    from_path=destination,
                    to_path=target / "deleted" / "2024/01/missing.avi",
                    sha256=expected_sha256,
                    operation="remove",
                )
                conn.commit()
            finally:
                conn.close()

            with patch("bildebank.cli.recover_pending_file_moves") as recover:
                code, stdout, stderr = capture_cli(
                    [
                        "--target",
                        str(target),
                        "repair-missing-file",
                        str(file_id),
                        str(candidate),
                        "--apply",
                    ]
                )

            self.assertEqual(code, 1, stdout)
            recover.assert_not_called()
            self.assertIn("uavklart(e) filflytting", stderr)
            self.assertFalse(destination.exists())
            self.assertTrue(candidate.exists())

    def test_apply_uses_safe_copy_fallback_when_hardlinks_are_unavailable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target, candidate, destination, file_id = (
                create_missing_file_fixture(Path(tmp))
            )

            with patch(
                "bildebank.missing_file_repair.os.link",
                side_effect=OSError(
                    errno.EOPNOTSUPP,
                    "hardlinks unsupported",
                ),
            ):
                code, stdout, stderr = capture_cli(
                    [
                        "--target",
                        str(target),
                        "repair-missing-file",
                        str(file_id),
                        str(candidate),
                        "--apply",
                    ]
                )

            self.assertEqual(code, 0, stderr)
            self.assertEqual(destination.read_bytes(), b"recovered-video")
            self.assertEqual(candidate.read_bytes(), b"recovered-video")

    def test_destination_race_never_overwrites_new_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target, candidate, destination, file_id = (
                create_missing_file_fixture(Path(tmp))
            )

            def create_destination_then_publish(
                temporary: Path,
                publish_destination: Path,
                *,
                expected_sha256: str,
                expected_size: int,
            ) -> None:
                publish_destination.write_bytes(b"new-file")
                publish_temporary_no_replace(
                    temporary,
                    publish_destination,
                    expected_sha256=expected_sha256,
                    expected_size=expected_size,
                )

            with patch(
                "bildebank.missing_file_repair.publish_temporary_no_replace",
                side_effect=create_destination_then_publish,
            ):
                code, stdout, stderr = capture_cli(
                    [
                        "--target",
                        str(target),
                        "repair-missing-file",
                        str(file_id),
                        str(candidate),
                        "--apply",
                    ]
                )

            self.assertEqual(code, 1, stdout)
            self.assertIn("blir ikke overskrevet", stderr)
            self.assertEqual(destination.read_bytes(), b"new-file")
            self.assertEqual(candidate.read_bytes(), b"recovered-video")
            self.assertEqual(
                list(destination.parent.glob(".*.bildebank-repair-*.tmp")),
                [],
            )
            self.assertFalse((target / LOCK_FILENAME).exists())


def create_missing_file_fixture(
    root: Path,
    *,
    deleted: bool = False,
) -> tuple[Path, Path, Path, int]:
    target = root / "collection"
    db.init_database(target)
    candidate = root / "recovered" / "restored-copy.avi"
    candidate.parent.mkdir()
    candidate.write_bytes(b"recovered-video")

    active_relative = Path("2024/01/missing.avi")
    active_path = target / active_relative
    active_path.parent.mkdir(parents=True)
    active_path.write_bytes(candidate.read_bytes())
    file_id = register_target_file(
        target,
        active_relative,
        source=candidate,
    )

    destination = active_path
    if deleted:
        deleted_relative = Path("deleted") / active_relative
        destination = target / deleted_relative
        destination.parent.mkdir(parents=True)
        active_path.rename(destination)
        conn = db.connect(target)
        try:
            conn.execute(
                """
                UPDATE files
                SET target_path = ?,
                    target_path_key = ?,
                    deleted_at = CURRENT_TIMESTAMP,
                    deleted_original_target_path = ?
                WHERE id = ?
                """,
                (
                    deleted_relative.as_posix(),
                    db.relative_path_key(deleted_relative),
                    active_relative.as_posix(),
                    file_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    destination.unlink()
    return target, candidate, destination, file_id
