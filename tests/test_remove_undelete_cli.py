from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bildebank.db import DB_FILENAME
from bildebank.media import sha256_file
from bildebank.server_actions import remove_file_from_browser, undelete_file_from_browser
from bildebank.server_browser import browser_item_by_id
from bildebank.target_lock import LOCK_FILENAME
from tests.test_cli import capture_cli, run_cli


class RemoveUndeleteCliTests(unittest.TestCase):
    def test_remove_moves_file_marks_database_and_hides_from_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            deleted = target / "deleted" / "2024" / "01" / "IMG_20240102.jpg"

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "remove", "2024/01/IMG_20240102.jpg"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Flyttet til slettet mappe", stdout)
            self.assertFalse(imported.exists())
            self.assertTrue(deleted.exists())
            self.assertEqual(deleted.read_bytes(), b"image-one")

            conn = sqlite3.connect(target / DB_FILENAME)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute("SELECT * FROM files").fetchone()
                self.assertEqual(Path(row["target_path"]), deleted.relative_to(target))
                self.assertEqual(Path(row["deleted_original_target_path"]), imported.relative_to(target))
                self.assertIsNotNone(row["deleted_at"])
            finally:
                conn.close()

            self.assertEqual(run_cli(["--target", str(target), "make-browser"]), 0)
            html = (target / "index.html").read_text(encoding="utf-8")
            self.assertNotIn("IMG_20240102.jpg", html)

            code, stdout, stderr = capture_cli(["--target", str(target), "list-removed"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("ja\t2024-01-02\tfilename", stdout)
            self.assertIn(str(imported.resolve()), stdout)
            self.assertIn(f"  slettet fil: {deleted.resolve()}", stdout)
            self.assertIn(f"  kildefil: {(source / 'IMG_20240102.jpg').resolve()}", stdout)
            self.assertIn("filstørrelse: 9 bytes (9 bytes)", stdout)
            self.assertIn("sha256:", stdout)

    def test_remove_stops_when_target_is_locked_without_changes(self) -> None:
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
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            lock_path = target / LOCK_FILENAME
            lock_path.write_text("command=import\npid=123\n", encoding="utf-8")
            with sqlite3.connect(target / DB_FILENAME) as conn:
                row_before = conn.execute(
                    "SELECT target_path, deleted_at FROM files WHERE id = 1"
                ).fetchone()
                commands_before = conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0]

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "remove", "2024/01/IMG_20240102.jpg"]
            )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Bildesamlingen er låst", stderr)
            self.assertTrue(imported.exists())
            with sqlite3.connect(target / DB_FILENAME) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT target_path, deleted_at FROM files WHERE id = 1"
                    ).fetchone(),
                    row_before,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0],
                    commands_before,
                )

    def test_remove_holds_lock_while_moving_and_releases_it(self) -> None:
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
            lock_path = target / LOCK_FILENAME
            observed_lock: list[bool] = []
            real_move = shutil.move

            def move_with_lock_check(source_path, destination_path):  # noqa: ANN001
                observed_lock.append(lock_path.exists())
                return real_move(source_path, destination_path)

            with patch(
                "bildebank.file_lifecycle.shutil.move",
                side_effect=move_with_lock_check,
            ):
                remove_file_from_browser(target, 1)

            self.assertEqual(observed_lock, [True])
            self.assertFalse(lock_path.exists())

    def test_remove_releases_lock_when_move_fails(self) -> None:
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
            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            lock_path = target / LOCK_FILENAME

            with (
                patch(
                    "bildebank.file_lifecycle.shutil.move",
                    side_effect=OSError("move failed"),
                ),
                self.assertRaisesRegex(OSError, "move failed"),
            ):
                remove_file_from_browser(target, 1)

            self.assertFalse(lock_path.exists())
            self.assertTrue(imported.exists())
            with sqlite3.connect(target / DB_FILENAME) as conn:
                row = conn.execute(
                    "SELECT target_path, deleted_at FROM files WHERE id = 1"
                ).fetchone()
            self.assertEqual(row, ("2024/01/IMG_20240102.jpg", None))

    def test_remove_completes_pending_move_row(self) -> None:
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

            self.assertEqual(run_cli(["--target", str(target), "remove", "2024/01/IMG_20240102.jpg"]), 0)

            with sqlite3.connect(target / DB_FILENAME) as conn:
                row = conn.execute(
                    "SELECT operation, state, completed_at FROM pending_file_moves"
                ).fetchone()
            self.assertEqual(row[0], "remove")
            self.assertEqual(row[1], "completed")
            self.assertIsNotNone(row[2])

    def test_remove_rejects_target_file_with_changed_content(self) -> None:
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
            imported.write_bytes(b"changed")

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "remove", "2024/01/IMG_20240102.jpg"]
            )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Fila på disk har feil SHA-256", stderr)
            self.assertTrue(imported.exists())
            self.assertFalse((target / "deleted" / "2024" / "01" / "IMG_20240102.jpg").exists())
            with sqlite3.connect(target / DB_FILENAME) as conn:
                row = conn.execute(
                    "SELECT target_path, deleted_at FROM files WHERE id = 1"
                ).fetchone()
                pending_count = conn.execute("SELECT COUNT(*) FROM pending_file_moves").fetchone()[0]
            self.assertEqual(row, ("2024/01/IMG_20240102.jpg", None))
            self.assertEqual(pending_count, 0)

    def test_remove_keeps_pending_move_when_post_move_hash_check_fails(self) -> None:
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
            expected_hash = sha256_file(imported)

            with (
                patch(
                    "bildebank.file_lifecycle.sha256_file",
                    side_effect=[expected_hash, "0" * 64],
                ),
                self.assertRaisesRegex(ValueError, "feil SHA-256"),
            ):
                remove_file_from_browser(target, 1)

            self.assertFalse(imported.exists())
            self.assertTrue(deleted.exists())
            with sqlite3.connect(target / DB_FILENAME) as conn:
                file_row = conn.execute(
                    "SELECT target_path, deleted_at FROM files WHERE id = 1"
                ).fetchone()
                move_row = conn.execute(
                    "SELECT operation, state, completed_at FROM pending_file_moves"
                ).fetchone()
            self.assertEqual(file_row, ("2024/01/IMG_20240102.jpg", None))
            self.assertEqual(move_row[0], "remove")
            self.assertEqual(move_row[1], "prepared")
            self.assertIsNone(move_row[2])

    def test_undelete_restores_removed_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "remove", "2024/01/IMG_20240102.jpg"]), 0)

            imported = target / "2024" / "01" / "IMG_20240102.jpg"
            deleted = target / "deleted" / "2024" / "01" / "IMG_20240102.jpg"
            code, stdout, stderr = capture_cli(
                ["--target", str(target), "undelete", "deleted/2024/01/IMG_20240102.jpg"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Flyttet tilbake til bildesamlingen", stdout)
            self.assertTrue(imported.exists())
            self.assertFalse(deleted.exists())
            self.assertEqual(imported.read_bytes(), b"image-one")
            conn = sqlite3.connect(target / DB_FILENAME)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute("SELECT target_path, deleted_at, deleted_original_target_path FROM files").fetchone()
            finally:
                conn.close()
            self.assertEqual(row["target_path"], "2024/01/IMG_20240102.jpg")
            self.assertIsNone(row["deleted_at"])
            self.assertIsNone(row["deleted_original_target_path"])
            self.assertIsNotNone(browser_item_by_id(target, 1))

    def test_undelete_stops_when_target_is_locked_without_changes(self) -> None:
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
            deleted = target / "deleted" / "2024" / "01" / "IMG_20240102.jpg"
            lock_path = target / LOCK_FILENAME
            lock_path.write_text("command=import\npid=123\n", encoding="utf-8")
            with sqlite3.connect(target / DB_FILENAME) as conn:
                row_before = conn.execute(
                    "SELECT target_path, deleted_at FROM files WHERE id = 1"
                ).fetchone()
                commands_before = conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0]

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "undelete",
                    "deleted/2024/01/IMG_20240102.jpg",
                ]
            )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Bildesamlingen er låst", stderr)
            self.assertTrue(deleted.exists())
            with sqlite3.connect(target / DB_FILENAME) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT target_path, deleted_at FROM files WHERE id = 1"
                    ).fetchone(),
                    row_before,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM command_log").fetchone()[0],
                    commands_before,
                )

    def test_undelete_holds_lock_while_moving_and_releases_it(self) -> None:
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
            lock_path = target / LOCK_FILENAME
            observed_lock: list[bool] = []
            real_move = shutil.move

            def move_with_lock_check(source_path, destination_path):  # noqa: ANN001
                observed_lock.append(lock_path.exists())
                return real_move(source_path, destination_path)

            with patch(
                "bildebank.file_lifecycle.shutil.move",
                side_effect=move_with_lock_check,
            ):
                undelete_file_from_browser(target, 1)

            self.assertEqual(observed_lock, [True])
            self.assertFalse(lock_path.exists())

    def test_undelete_rejects_original_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "remove", "2024/01/IMG_20240102.jpg"]), 0)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "undelete", "2024/01/IMG_20240102.jpg"]
            )

            self.assertNotEqual(code, 0)
            self.assertIn("Undelete krever sti under deleted/", stderr)
            self.assertFalse((target / "2024" / "01" / "IMG_20240102.jpg").exists())
            self.assertTrue((target / "deleted" / "2024" / "01" / "IMG_20240102.jpg").exists())

    def test_undelete_fails_when_destination_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "remove", "2024/01/IMG_20240102.jpg"]), 0)
            destination = target / "2024" / "01" / "IMG_20240102.jpg"
            destination.write_bytes(b"already-here")

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "undelete", "deleted/2024/01/IMG_20240102.jpg"]
            )

            self.assertNotEqual(code, 0)
            self.assertIn("Målfilen finnes allerede", stderr)
            self.assertEqual(destination.read_bytes(), b"already-here")
            self.assertTrue((target / "deleted" / "2024" / "01" / "IMG_20240102.jpg").exists())

    def test_undelete_fails_when_deleted_file_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "remove", "2024/01/IMG_20240102.jpg"]), 0)
            (target / "deleted" / "2024" / "01" / "IMG_20240102.jpg").unlink()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "undelete", "deleted/2024/01/IMG_20240102.jpg"]
            )

            self.assertNotEqual(code, 0)
            self.assertIn("Slettet fil finnes ikke på disk", stderr)
            self.assertFalse((target / "2024" / "01" / "IMG_20240102.jpg").exists())

    def test_undelete_rejects_deleted_file_with_changed_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "remove", "2024/01/IMG_20240102.jpg"]), 0)
            deleted = target / "deleted" / "2024" / "01" / "IMG_20240102.jpg"
            restored = target / "2024" / "01" / "IMG_20240102.jpg"
            deleted.write_bytes(b"changed")

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "undelete", "deleted/2024/01/IMG_20240102.jpg"]
            )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Fila på disk har feil SHA-256", stderr)
            self.assertTrue(deleted.exists())
            self.assertFalse(restored.exists())
            with sqlite3.connect(target / DB_FILENAME) as conn:
                row = conn.execute(
                    "SELECT target_path, deleted_at FROM files WHERE id = 1"
                ).fetchone()
                pending_count = conn.execute(
                    "SELECT COUNT(*) FROM pending_file_moves WHERE state = 'prepared'"
                ).fetchone()[0]
            self.assertEqual(row[0], "deleted/2024/01/IMG_20240102.jpg")
            self.assertIsNotNone(row[1])
            self.assertEqual(pending_count, 0)

    def test_undelete_fails_when_database_row_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            deleted = target / "deleted" / "2024" / "01" / "IMG_20240102.jpg"
            deleted.parent.mkdir(parents=True)
            deleted.write_bytes(b"image-one")

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "undelete", "deleted/2024/01/IMG_20240102.jpg"]
            )

            self.assertNotEqual(code, 0)
            self.assertIn("Filen finnes ikke i importdatabasen", stderr)
            self.assertTrue(deleted.exists())
