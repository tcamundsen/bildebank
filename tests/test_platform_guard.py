from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.cli import main
from bildebank.platform_guard import (
    MountInfo,
    is_windows_mount,
    mount_for_path,
    validate_collection_platform,
)


BLOCK_MESSAGE = "Bildebank kan ikke brukes fra WSL"


def capture_cli(args: list[str]) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(args)
    return code, stdout.getvalue(), stderr.getvalue()


class PlatformGuardTests(unittest.TestCase):
    def test_windows_mount_recognizes_drvfs_and_wsl2_9p(self) -> None:
        self.assertTrue(
            is_windows_mount(MountInfo(Path("/mnt/c"), "drvfs", "C:", ()))
        )
        self.assertTrue(
            is_windows_mount(
                MountInfo(
                    Path("/windows"),
                    "9p",
                    r"C:\134",
                    ("rw", r"aname=drvfs;path=C:\;uid=1000"),
                )
            )
        )
        self.assertFalse(
            is_windows_mount(MountInfo(Path("/"), "ext4", "/dev/sda", ("rw",)))
        )

    def test_mount_for_path_uses_longest_matching_mount(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "windows" / "collection"
            target.mkdir(parents=True)
            mounts = (
                MountInfo(Path("/"), "ext4", "/dev/sda", ("rw",)),
                MountInfo(root / "windows", "9p", r"C:\134", ("rw", "aname=drvfs")),
            )
            with patch("bildebank.platform_guard.read_mountinfo", return_value=mounts):
                selected = mount_for_path(target)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.mount_point, root / "windows")

    def test_validate_rejects_windows_collection_in_wsl(self) -> None:
        mount = MountInfo(Path("/mnt/c"), "9p", r"C:\134", ("rw", "aname=drvfs"))
        with (
            patch("bildebank.platform_guard.is_wsl", return_value=True),
            patch("bildebank.platform_guard.mount_for_path", return_value=mount),
            self.assertRaisesRegex(ValueError, BLOCK_MESSAGE),
        ):
            validate_collection_platform(Path("/mnt/c/Bilder"))

    def test_validate_allows_linux_collection_in_wsl(self) -> None:
        mount = MountInfo(Path("/"), "ext4", "/dev/sda", ("rw",))
        with (
            patch("bildebank.platform_guard.is_wsl", return_value=True),
            patch("bildebank.platform_guard.mount_for_path", return_value=mount),
        ):
            validate_collection_platform(Path("/home/user/Bilder"))

    def test_validate_does_not_inspect_mounts_outside_wsl(self) -> None:
        with (
            patch("bildebank.platform_guard.is_wsl", return_value=False),
            patch("bildebank.platform_guard.mount_for_path") as mount_for_target,
        ):
            validate_collection_platform(Path("/mnt/c/Bilder"))

        mount_for_target.assert_not_called()

    def test_create_is_blocked_before_database_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "collection"
            with patch(
                "bildebank.cli.validate_collection_platform",
                side_effect=ValueError(BLOCK_MESSAGE),
            ):
                code, stdout, stderr = capture_cli(["create", str(target)])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn(BLOCK_MESSAGE, stderr)
            self.assertFalse(db.db_path_for_target(target).exists())

    def test_target_commands_are_blocked_before_program_state_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "collection"
            db.init_database(target)
            with (
                patch(
                    "bildebank.cli.validate_collection_platform",
                    side_effect=ValueError(BLOCK_MESSAGE),
                ),
                patch("bildebank.cli.record_target_best_effort") as record_target,
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "status"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn(BLOCK_MESSAGE, stderr)
            record_target.assert_not_called()

    def test_run_server_is_blocked_by_common_target_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "collection"
            db.init_database(target)
            with (
                patch(
                    "bildebank.cli.validate_collection_platform",
                    side_effect=ValueError(BLOCK_MESSAGE),
                ),
                patch("bildebank.cli.run_server_command") as run_server,
            ):
                code, _, stderr = capture_cli(
                    ["--target", str(target), "run-server", "--no-browser"]
                )

            self.assertEqual(code, 1)
            self.assertIn(BLOCK_MESSAGE, stderr)
            run_server.assert_not_called()

    def test_doctor_is_blocked_before_collecting_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "collection"
            db.init_database(target)
            with (
                patch(
                    "bildebank.cli.validate_collection_platform",
                    side_effect=ValueError(BLOCK_MESSAGE),
                ),
                patch("bildebank.cli.load_config") as load_config,
            ):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "doctor"]
                )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn(BLOCK_MESSAGE, stderr)
            load_config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
