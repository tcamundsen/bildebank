from __future__ import annotations

import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bildebank.media import sha256_file
from bildebank.safe_file_move import move_file_no_replace


class SafeFileMoveTests(unittest.TestCase):
    def test_move_refuses_to_overwrite_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            destination = root / "destination.jpg"
            source.write_bytes(b"source")
            destination.write_bytes(b"destination")

            with self.assertRaisesRegex(FileExistsError, "blir ikke overskrevet"):
                move_file_no_replace(
                    source,
                    destination,
                    expected_sha256=sha256_file(source),
                )

            self.assertEqual(source.read_bytes(), b"source")
            self.assertEqual(destination.read_bytes(), b"destination")

    def test_move_preserves_expected_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            destination = root / "destination.jpg"
            source.write_bytes(b"source")
            expected_sha256 = sha256_file(source)

            move_file_no_replace(
                source,
                destination,
                expected_sha256=expected_sha256,
            )

            self.assertFalse(source.exists())
            self.assertEqual(destination.read_bytes(), b"source")
            self.assertEqual(sha256_file(destination), expected_sha256)

    def test_windows_rename_race_keeps_both_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            destination = root / "destination.jpg"
            source.write_bytes(b"source")

            def destination_appears(*_args) -> None:
                destination.write_bytes(b"destination")
                raise FileExistsError("destination appeared")

            with (
                patch("bildebank.safe_file_move._is_windows", return_value=True),
                patch(
                    "bildebank.safe_file_move.os.rename",
                    side_effect=destination_appears,
                ),
                self.assertRaisesRegex(FileExistsError, "blir ikke overskrevet"),
            ):
                move_file_no_replace(
                    source,
                    destination,
                    expected_sha256=sha256_file(source),
                )

            self.assertEqual(source.read_bytes(), b"source")
            self.assertEqual(destination.read_bytes(), b"destination")

    @unittest.skipIf(os.name == "nt", "POSIX-link brukes ikke på Windows")
    def test_posix_link_race_keeps_both_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            destination = root / "destination.jpg"
            source.write_bytes(b"source")

            def destination_appears(*_args, **_kwargs) -> None:
                destination.write_bytes(b"destination")
                raise FileExistsError("destination appeared")

            with (
                patch(
                    "bildebank.safe_file_move.os.link",
                    side_effect=destination_appears,
                ),
                self.assertRaisesRegex(FileExistsError, "blir ikke overskrevet"),
            ):
                move_file_no_replace(
                    source,
                    destination,
                    expected_sha256=sha256_file(source),
                )

            self.assertEqual(source.read_bytes(), b"source")
            self.assertEqual(destination.read_bytes(), b"destination")

    @unittest.skipIf(os.name == "nt", "POSIX-fallback brukes ikke på Windows")
    def test_move_falls_back_to_exclusive_copy_when_hardlinks_are_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            destination = root / "destination.jpg"
            source.write_bytes(b"source")
            expected_sha256 = sha256_file(source)

            with patch(
                "bildebank.safe_file_move.os.link",
                side_effect=OSError(errno.EOPNOTSUPP, "hardlinks støttes ikke"),
            ):
                move_file_no_replace(
                    source,
                    destination,
                    expected_sha256=expected_sha256,
                )

            self.assertFalse(source.exists())
            self.assertEqual(destination.read_bytes(), b"source")
