from __future__ import annotations

import os
import stat
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from bildebank.target_lock import (
    LOCK_FILENAME,
    TargetLock,
    TargetLockError,
    lock_details,
)


class TargetLockTests(unittest.TestCase):
    def test_lock_is_private_exclusive_and_removed_after_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            lock_path = target / LOCK_FILENAME

            with TargetLock(target, command="test"):
                details = dict(
                    line.split("=", 1)
                    for line in lock_path.read_text(encoding="utf-8").splitlines()
                )
                self.assertEqual(details["command"], "test")
                uuid.UUID(details["owner_id"])
                if os.name != "nt":
                    self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)
                with self.assertRaises(TargetLockError):
                    with TargetLock(target, command="other"):
                        self.fail("En konkurrerende target-lås skal ikke kunne tas")

            self.assertFalse(lock_path.exists())

    def test_interrupted_lock_write_removes_incomplete_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            with (
                patch("bildebank.target_lock._write_all", side_effect=KeyboardInterrupt),
                self.assertRaises(KeyboardInterrupt),
            ):
                TargetLock(target, command="test").__enter__()

            self.assertFalse((target / LOCK_FILENAME).exists())

    def test_release_does_not_remove_replacement_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            lock_path = target / LOCK_FILENAME
            lock = TargetLock(target, command="old")
            lock.__enter__()
            owned_fd = lock.fd
            replacement = "owner_id=ny\ncommand=new\n"
            real_close = os.close
            replaced = False

            def close_then_replace(fd: int) -> None:
                nonlocal replaced
                real_close(fd)
                if fd == owned_fd and not replaced:
                    replaced = True
                    lock_path.unlink()
                    lock_path.write_text(replacement, encoding="utf-8")

            with (
                patch("bildebank.target_lock.os.close", side_effect=close_then_replace),
                self.assertRaises(TargetLockError),
            ):
                lock.__exit__(None, None, None)

            self.assertEqual(lock_path.read_text(encoding="utf-8"), replacement)

    def test_lock_details_does_not_follow_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / LOCK_FILENAME
            secret = root / "secret.txt"
            secret.write_text("skal ikke vises", encoding="utf-8")
            try:
                lock_path.symlink_to(secret)
            except OSError as exc:
                self.skipTest(f"Kan ikke opprette testlenke på denne plattformen: {exc}")

            self.assertNotIn("skal ikke vises", lock_details(lock_path))

    def test_lock_details_omits_large_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / LOCK_FILENAME
            lock_path.write_bytes(b"x" * 4097)

            self.assertNotIn("xxxx", lock_details(lock_path))

    def test_lock_details_escapes_terminal_controls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / LOCK_FILENAME
            lock_path.write_text("command=test\x1b[31m\n", encoding="utf-8")

            details = lock_details(lock_path)
            self.assertNotIn("\x1b", details)
            self.assertIn("\\x1b", details)


if __name__ == "__main__":
    unittest.main()
