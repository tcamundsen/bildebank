from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bildebank.browser_output import (
    BROWSER_MARKER_FILENAME,
    PEOPLE_PREVIOUS_DIRECTORY_NAME,
    PEOPLE_STAGING_DIRECTORY_PREFIX,
    create_people_staging_directory,
    discard_people_staging_directory,
    ensure_people_root,
    publish_browser_index,
    publish_people_directory,
    replace_text_file,
    write_new_text_file,
)


class BrowserOutputTests(unittest.TestCase):
    def test_browser_root_requires_bildebank_ownership_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            browser = target / "browser"
            browser.mkdir(parents=True)
            unrelated = browser / "privat.txt"
            unrelated.write_text("behold", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "eierskapsmarkør"):
                publish_browser_index(target, "ny browser")

            self.assertEqual(unrelated.read_text(encoding="utf-8"), "behold")
            self.assertFalse((browser / "index.html").exists())

    def test_browser_index_never_follows_existing_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            output = publish_browser_index(target, "første")
            output.unlink()
            victim = target / ".bilder.sqlite3"
            victim.write_bytes(b"database")
            try:
                output.symlink_to(victim)
            except OSError as exc:
                self.skipTest(f"Kan ikke opprette symlink på denne plattformen: {exc}")

            with self.assertRaisesRegex(ValueError, "uten lenker"):
                publish_browser_index(target, "skal ikke skrives")

            self.assertEqual(victim.read_bytes(), b"database")
            self.assertTrue(output.is_symlink())

    def test_browser_root_never_follows_existing_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            victim = root / "victim"
            victim.mkdir()
            try:
                (target / "browser").symlink_to(victim, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Kan ikke opprette symlink på denne plattformen: {exc}")

            with self.assertRaisesRegex(ValueError, "uten lenker"):
                publish_browser_index(target, "skal ikke skrives")

            self.assertEqual(list(victim.iterdir()), [])
            self.assertTrue((target / "browser").is_symlink())

    def test_atomic_file_failure_keeps_previous_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            output = publish_browser_index(target, "gammel")

            with patch(
                "bildebank.browser_output.os.replace",
                side_effect=OSError("simulert publiseringsfeil"),
            ):
                with self.assertRaisesRegex(OSError, "simulert"):
                    replace_text_file(output, "ny")

            self.assertEqual(output.read_text(encoding="utf-8"), "gammel")
            self.assertEqual(
                list(output.parent.glob(f".{output.name}.tmp-*")),
                [],
            )

    def test_people_publish_rolls_back_if_new_directory_cannot_be_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            first_staging = create_people_staging_directory(target)
            write_new_text_file(first_staging / "index.html", "gammel")
            publish_people_directory(target, first_staging)

            second_staging = create_people_staging_directory(target)
            write_new_text_file(second_staging / "index.html", "ny")
            real_rename = Path.rename

            def fail_new_publish(path: Path, destination: Path) -> Path:
                if path == second_staging:
                    raise OSError("simulert katalogfeil")
                return real_rename(path, destination)

            with patch("pathlib.Path.rename", fail_new_publish):
                with self.assertRaisesRegex(OSError, "simulert"):
                    publish_people_directory(target, second_staging)

            people = target / "browser" / "people"
            self.assertEqual((people / "index.html").read_text(encoding="utf-8"), "gammel")
            self.assertFalse((target / "browser" / PEOPLE_PREVIOUS_DIRECTORY_NAME).exists())
            discard_people_staging_directory(second_staging)

    def test_people_recovery_restores_previous_and_discards_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            first_staging = create_people_staging_directory(target)
            write_new_text_file(first_staging / "index.html", "gammel")
            people = publish_people_directory(target, first_staging)
            previous = people.with_name(PEOPLE_PREVIOUS_DIRECTORY_NAME)
            people.rename(previous)
            incomplete = people.with_name(f"{PEOPLE_STAGING_DIRECTORY_PREFIX}avbrutt")
            incomplete.mkdir()
            write_new_text_file(incomplete / "index.html", "ufullstendig")

            recovered = ensure_people_root(target)

            self.assertEqual(recovered, people)
            self.assertEqual((recovered / "index.html").read_text(encoding="utf-8"), "gammel")
            self.assertFalse(previous.exists())
            self.assertFalse(incomplete.exists())
            self.assertTrue(
                (target / "browser" / BROWSER_MARKER_FILENAME).is_file()
            )
