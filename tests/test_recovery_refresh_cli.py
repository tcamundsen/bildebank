from __future__ import annotations

import datetime as dt
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.db import DB_FILENAME
from bildebank.media import sha256_file
from bildebank.safe_file_move import move_file_no_replace
from bildebank.target_lock import LOCK_FILENAME
from tests.cli_helpers import capture_cli, run_cli
from tests.test_media import jpeg_with_exif_camera, minimal_avi_with_idit_outside_info


class RecoveryRefreshCliTests(unittest.TestCase):
    def test_recovery_completes_remove_after_file_was_moved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")
            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            deleted = target / "deleted" / "2024" / "01" / "IMG_20240102.jpg"
            real_move = move_file_no_replace

            def move_then_crash(source_path, destination_path, *, expected_sha256):  # noqa: ANN001
                real_move(
                    source_path,
                    destination_path,
                    expected_sha256=expected_sha256,
                )
                raise RuntimeError("crash after move")

            with patch(
                "bildebank.file_lifecycle.move_file_no_replace",
                side_effect=move_then_crash,
            ):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "remove", "2024/01/IMG_20240102.jpg"]
                )

            self.assertEqual(code, 1)
            self.assertFalse(imported.exists())
            self.assertTrue(deleted.exists())
            code, stdout, stderr = capture_cli(["--target", str(target), "status"])

            self.assertEqual(code, 0, stderr)
            with closing(sqlite3.connect(target / DB_FILENAME)) as conn, conn:
                row = conn.execute(
                    """
                    SELECT files.target_path, files.deleted_at, pending_file_moves.state
                    FROM files
                    JOIN pending_file_moves ON pending_file_moves.file_id = files.id
                    """
                ).fetchone()
            self.assertEqual(row[0], "deleted/2024/01/IMG_20240102.jpg")
            self.assertIsNotNone(row[1])
            self.assertEqual(row[2], "completed")

    def test_recovery_aborts_remove_when_file_was_not_moved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")
            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            imported = target / "2024" / "01" / "IMG_20240102.jpg"

            with patch(
                "bildebank.file_lifecycle.move_file_no_replace",
                side_effect=OSError("move failed"),
            ):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "remove", "2024/01/IMG_20240102.jpg"]
                )

            self.assertEqual(code, 1)
            self.assertTrue(imported.exists())
            code, stdout, stderr = capture_cli(["--target", str(target), "status"])

            self.assertEqual(code, 0, stderr)
            with closing(sqlite3.connect(target / DB_FILENAME)) as conn, conn:
                row = conn.execute(
                    """
                    SELECT files.target_path, files.deleted_at, pending_file_moves.state
                    FROM files
                    JOIN pending_file_moves ON pending_file_moves.file_id = files.id
                    """
                ).fetchone()
            self.assertEqual(row[0], "2024/01/IMG_20240102.jpg")
            self.assertIsNone(row[1])
            self.assertEqual(row[2], "aborted")

    def test_recovery_completes_undelete_after_file_was_moved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")
            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(
                    [
                        "--target",
                        str(target),
                        "import",
                        "--name",
                        source.name,
                        "--quiet",
                        str(source),
                    ]
                ),
                0,
            )
            self.assertEqual(
                run_cli(
                    [
                        "--target",
                        str(target),
                        "remove",
                        "2024/01/IMG_20240102.jpg",
                    ]
                ),
                0,
            )
            restored = target / "2024" / "01" / "IMG_20240102.jpg"
            deleted = target / "deleted" / "2024" / "01" / "IMG_20240102.jpg"
            with closing(sqlite3.connect(target / DB_FILENAME)) as conn, conn:
                db.create_pending_file_move(
                    conn,
                    file_id=1,
                    target_root=target,
                    from_path=deleted,
                    to_path=restored,
                    sha256=sha256_file(deleted),
                    operation="undelete",
                )
                conn.commit()
            shutil.move(str(deleted), str(restored))

            code, stdout, stderr = capture_cli(["--target", str(target), "status"])

            self.assertEqual(code, 0, stderr)
            self.assertTrue(restored.exists())
            self.assertFalse(deleted.exists())
            with closing(sqlite3.connect(target / DB_FILENAME)) as conn, conn:
                row = conn.execute(
                    """
                    SELECT files.target_path, files.deleted_at,
                           files.deleted_original_target_path,
                           pending_file_moves.state
                    FROM files
                    JOIN pending_file_moves ON pending_file_moves.file_id = files.id
                    WHERE pending_file_moves.operation = 'undelete'
                    """
                ).fetchone()
            self.assertEqual(row[0], "2024/01/IMG_20240102.jpg")
            self.assertIsNone(row[1])
            self.assertIsNone(row[2])
            self.assertEqual(row[3], "completed")

    def test_recovery_stops_when_both_move_paths_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")
            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            deleted = target / "deleted" / "2024" / "01" / "IMG_20240102.jpg"
            deleted.parent.mkdir(parents=True)
            deleted.write_bytes(b"image-one")
            with closing(sqlite3.connect(target / DB_FILENAME)) as conn, conn:
                db.create_pending_file_move(
                    conn,
                    file_id=1,
                    target_root=target,
                    from_path=imported,
                    to_path=deleted,
                    sha256=sha256_file(imported),
                    operation="remove",
                )
                conn.commit()

            code, stdout, stderr = capture_cli(["--target", str(target), "status"])

            self.assertEqual(code, 1)
            self.assertIn("både kilde og mål finnes", stderr)

    def test_recovery_stops_when_moved_file_hash_does_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")
            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            deleted = target / "deleted" / "2024" / "01" / "IMG_20240102.jpg"
            deleted.parent.mkdir(parents=True)
            shutil.move(str(imported), str(deleted))
            deleted.write_bytes(b"changed")
            with closing(sqlite3.connect(target / DB_FILENAME)) as conn, conn:
                db.create_pending_file_move(
                    conn,
                    file_id=1,
                    target_root=target,
                    from_path=imported,
                    to_path=deleted,
                    sha256="0" * 64,
                    operation="remove",
                )
                conn.commit()

            code, stdout, stderr = capture_cli(["--target", str(target), "status"])

            self.assertEqual(code, 1)
            self.assertIn("forventet " + "0" * 64, stderr)

    def test_refresh_metadata_moves_non_metadata_file_when_metadata_becomes_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "video.avi"
            source_file.write_bytes(b"RIFF\x04\x00\x00\x00AVI ")
            old_time = dt.datetime(2008, 2, 29, 12, 0).timestamp()
            os.utime(source_file, (old_time, old_time))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            old_target = target / "2008" / "02" / "video.avi"
            new_target = target / "2007" / "03" / "video.avi"
            self.assertTrue(old_target.exists())

            old_target.write_bytes(minimal_avi_with_idit_outside_info())
            with closing(sqlite3.connect(target / DB_FILENAME)) as conn, conn:
                conn.execute(
                    "UPDATE files SET sha256 = ?, size_bytes = ?",
                    (sha256_file(old_target), old_target.stat().st_size),
                )
                conn.commit()

            code, stdout, stderr = capture_cli(["--target", str(target), "refresh-metadata"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Refresh-metadata: kontrollert=1/1", stdout)
            self.assertIn("gjenstår=0s", stdout)
            self.assertIn("flyttet=1", stdout)
            self.assertFalse(old_target.exists())
            self.assertTrue(new_target.exists())

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                row = conn.execute(
                    "SELECT target_path, taken_date, date_source FROM files"
                ).fetchone()
                self.assertEqual(row[0], new_target.relative_to(target).as_posix())
                self.assertEqual(row[1], "2007-03-12")
                self.assertEqual(row[2], "metadata")
            finally:
                conn.close()

    def test_refresh_metadata_rejects_changed_content_before_moving(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "video.avi"
            source_file.write_bytes(b"RIFF\x04\x00\x00\x00AVI ")
            old_time = dt.datetime(2008, 2, 29, 12, 0).timestamp()
            os.utime(source_file, (old_time, old_time))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(
                    [
                        "--target",
                        str(target),
                        "import",
                        "--name",
                        source.name,
                        "--quiet",
                        str(source),
                    ]
                ),
                0,
            )
            old_target = target / "2008" / "02" / "video.avi"
            new_target = target / "2007" / "03" / "video.avi"
            old_target.write_bytes(minimal_avi_with_idit_outside_info())

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "refresh-metadata", "--verbose"]
            )

            self.assertEqual(code, 2, stderr)
            self.assertIn("feil SHA-256", stdout)
            self.assertTrue(old_target.exists())
            self.assertFalse(new_target.exists())
            with closing(sqlite3.connect(target / DB_FILENAME)) as conn, conn:
                row = conn.execute(
                    "SELECT target_path, date_source FROM files"
                ).fetchone()
                pending_count = conn.execute(
                    "SELECT COUNT(*) FROM pending_file_moves"
                ).fetchone()[0]
            self.assertEqual(row, ("2008/02/video.avi", "mtime"))
            self.assertEqual(pending_count, 0)

    def test_recovery_completes_refresh_metadata_after_file_was_moved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "video.avi"
            source_file.write_bytes(b"RIFF\x04\x00\x00\x00AVI ")
            old_time = dt.datetime(2008, 2, 29, 12, 0).timestamp()
            os.utime(source_file, (old_time, old_time))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )

            old_target = target / "2008" / "02" / "video.avi"
            new_target = target / "2007" / "03" / "video.avi"
            old_target.write_bytes(minimal_avi_with_idit_outside_info())
            with closing(sqlite3.connect(target / DB_FILENAME)) as conn, conn:
                conn.execute("UPDATE files SET sha256 = ?", (sha256_file(old_target),))
                conn.commit()
            real_move = move_file_no_replace

            def move_then_crash(source_path, destination_path, *, expected_sha256):  # noqa: ANN001
                real_move(
                    source_path,
                    destination_path,
                    expected_sha256=expected_sha256,
                )
                raise RuntimeError("crash after move")

            with patch(
                "bildebank.importer.move_file_no_replace",
                side_effect=move_then_crash,
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "refresh-metadata"])

            self.assertEqual(code, 2)
            self.assertFalse(old_target.exists())
            self.assertTrue(new_target.exists())
            code, stdout, stderr = capture_cli(["--target", str(target), "status"])

            self.assertEqual(code, 0, stderr)
            with closing(sqlite3.connect(target / DB_FILENAME)) as conn, conn:
                row = conn.execute(
                    """
                    SELECT files.target_path, files.taken_date, files.date_source,
                           pending_file_moves.state
                    FROM files
                    JOIN pending_file_moves ON pending_file_moves.file_id = files.id
                    """
                ).fetchone()
            self.assertEqual(row[0], "2007/03/video.avi")
            self.assertEqual(row[1], "2007-03-12")
            self.assertEqual(row[2], "metadata")
            self.assertEqual(row[3], "completed")

    def test_refresh_metadata_refuses_to_run_while_target_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            (target / LOCK_FILENAME).write_text("command=remove\n", encoding="utf-8")

            with (
                patch("bildebank.cli.recover_pending_file_moves"),
                patch("bildebank.cli.db.connect", side_effect=AssertionError("db before lock")),
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "refresh-metadata"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Bildesamlingen er låst", stderr)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "refresh-metadata", "--dry-run"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Dry-run", stdout)

    def test_refresh_metadata_rescan_fills_camera_for_existing_metadata_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "IMG_20240102.jpg"
            source_file.write_bytes(jpeg_with_exif_camera("Canon", "EOS 80D"))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute(
                    """
                    UPDATE files
                    SET date_source = 'metadata',
                        camera_make = NULL,
                        camera_model = NULL
                    """
                )
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "refresh-metadata"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("sjekket=0", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("SELECT camera_make, camera_model FROM files").fetchone(),
                    (None, None),
                )
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "refresh-metadata", "--rescan"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("sjekket=1", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                self.assertEqual(
                    conn.execute("SELECT camera_make, camera_model FROM files").fetchone(),
                    ("Canon", "EOS 80D"),
                )
            finally:
                conn.close()

    def test_refresh_metadata_rescan_commits_progress_when_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(jpeg_with_exif_camera("Canon", "EOS 80D"))
            (source / "IMG_20240103.jpg").write_bytes(jpeg_with_exif_camera("Apple", "iPhone 17"))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute(
                    """
                    UPDATE files
                    SET date_source = 'metadata',
                        camera_make = NULL,
                        camera_model = NULL
                    """
                )
                conn.commit()
            finally:
                conn.close()

            calls = 0

            def interrupt_after_one(conn, target, row, stats, *, dry_run, verbose):
                nonlocal calls
                calls += 1
                if calls == 1:
                    db.update_file_camera(
                        conn,
                        file_id=int(row["id"]),
                        camera_make="Canon",
                        camera_model="EOS 80D",
                    )
                    return
                raise KeyboardInterrupt

            with patch("bildebank.importer.refresh_non_metadata_file", side_effect=interrupt_after_one):
                code, stdout, stderr = capture_cli(["--target", str(target), "refresh-metadata", "--rescan"])

            self.assertEqual(code, 130, stderr)
            self.assertIn("Avbrutt. Databaseendringer er lagret", stdout)
            self.assertIn("avbrutt=ja", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                rows = conn.execute(
                    "SELECT camera_make, camera_model FROM files ORDER BY target_path"
                ).fetchall()
                self.assertEqual(rows[0], ("Canon", "EOS 80D"))
                self.assertEqual(rows[1], (None, None))
            finally:
                conn.close()

    def test_refresh_metadata_verbose_prints_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "IMG_20240102.jpg"
            source_file.write_bytes(b"filename-date")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            imported.unlink()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "refresh-metadata", "--verbose"]
            )

            self.assertEqual(code, 2, stderr)
            self.assertIn("FEIL", stdout)
            self.assertIn("Filen finnes ikke", stdout)

    def test_refresh_metadata_resolves_missing_file_error_when_file_exists_without_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            source_file = source / "IMG_20240102.jpg"
            source_file.write_bytes(b"filename-date")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                conn.execute(
                    "insert into errors(stage, source_path, message) values(?, ?, ?)",
                    ("refresh-metadata", str(imported), f"Målfil finnes ikke: {imported}"),
                )
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "refresh-metadata", "--verbose"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("INGEN_METADATA", stdout)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                unresolved = conn.execute(
                    "select count(*) from errors where resolved_at is null"
                ).fetchone()[0]
                self.assertEqual(unresolved, 0)
            finally:
                conn.close()

    def test_refresh_metadata_reports_missing_target_path_without_hash_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            old_target = target / "2008" / "02" / "video.avi"
            repaired_target = target / "2007" / "03" / "video.avi"

            self.assertEqual(run_cli(["create", str(target)]), 0)
            repaired_target.parent.mkdir(parents=True)
            repaired_target.write_bytes(minimal_avi_with_idit_outside_info())
            file_hash = sha256_file(repaired_target)

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                source_id = conn.execute(
                    "insert into sources(path, path_key, name) values(?, ?, 'source') returning id",
                    (str(root / "source"), str(root / "source")),
                ).fetchone()[0]
                file_id = conn.execute(
                    """
                    insert into files(
                        target_path, target_path_key, original_filename, stored_filename, sha256,
                        size_bytes, taken_date, date_source, name_conflict
                    ) values(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    returning id
                    """,
                    (
                        old_target.relative_to(target).as_posix(),
                        old_target.relative_to(target).as_posix(),
                        "video.avi",
                        "video.avi",
                        file_hash,
                        repaired_target.stat().st_size,
                        "2008-02-29",
                        "mtime",
                        0,
                    ),
                ).fetchone()[0]
                conn.execute(
                    """
                    insert into file_sources(
                        file_id, source_id, source_path, source_path_key, sha256, size_bytes
                    ) values(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_id,
                        source_id,
                        str(root / "source" / "video.avi"),
                        str(root / "source" / "video.avi"),
                        file_hash,
                        repaired_target.stat().st_size,
                    ),
                )
                conn.execute(
                    "insert into errors(stage, source_path, message) values(?, ?, ?)",
                    ("refresh-metadata", str(old_target), "Filen finnes ikke"),
                )
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "refresh-metadata", "--verbose"]
            )

            self.assertEqual(code, 2, stderr)
            self.assertIn("FEIL", stdout)
            self.assertIn("Filen finnes ikke", stdout)
            self.assertNotIn("REPARERER_DB_PATH", stdout)
            self.assertTrue(repaired_target.exists())
            self.assertFalse(old_target.exists())

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                row = conn.execute(
                    "select target_path, taken_date, date_source from files"
                ).fetchone()
                self.assertEqual(row[0], old_target.relative_to(target).as_posix())
                self.assertEqual(row[1], "2008-02-29")
                self.assertEqual(row[2], "mtime")
                unresolved = conn.execute(
                    "select count(*) from errors where resolved_at is null"
                ).fetchone()[0]
                self.assertEqual(unresolved, 2)
            finally:
                conn.close()
