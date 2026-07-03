from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from bildebank.cli import main
from bildebank.config import BrowserConfig, BrowserHotkeyConfig, load_config
from tests.cli_helpers import capture_cli


class ConfigCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.program_root_tempdir = tempfile.TemporaryDirectory()
        self.program_root = Path(self.program_root_tempdir.name)
        self.program_root_patcher = patch("bildebank.cli.program_repo_root", return_value=self.program_root)
        self.program_root_patcher.start()

    def tearDown(self) -> None:
        self.program_root_patcher.stop()
        self.program_root_tempdir.cleanup()

    def test_config_help_preserves_description_examples(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["config", "-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("Slå valgfrie funksjoner på eller av i bildebank-config.toml.\nEksempel:\n\n", stdout)
        self.assertIn(" bildebank config face_recognition enable\n", stdout)
        self.assertIn(" bildebank config image_search disable\n", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_load_config_reads_tag_hotkey(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bildebank-config.toml").write_text(
                '[browser.hotkeys]\n"1" = { action = "tag", tag_name = "Familie" }\n',
                encoding="utf-8",
            )

            config = load_config(root)

        self.assertEqual(config.browser.hotkeys["1"], BrowserHotkeyConfig(action="tag", tag_name="Familie"))

    def test_config_enables_face_recognition(self) -> None:
        code, stdout, stderr = capture_cli(["config", "face_recognition", "enable"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("face_recognition.enabled er satt til true.", stdout)
        self.assertIn("Config-fil:", stdout)
        config = load_config(self.program_root)
        self.assertTrue(config.face_recognition.enabled)

    def test_config_updates_image_search_without_changing_other_fields(self) -> None:
        (self.program_root / "bildebank-config.toml").write_text(
            """
[face_recognition]
enabled = true
provider = "cpu"
model_root = "models/insightface"
database_dir = ".faces-by-model"
model_name = "buffalo_s"

[openclip]
enabled = true
model_root = "models/openclip"
device = "cpu"
model_name = "ViT-L-14"
pretrained = "laion2b_s32b_b82k"
""",
            encoding="utf-8",
        )

        code, stdout, stderr = capture_cli(["config", "image_search", "disable"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("image_search.enabled er satt til false.", stdout)
        config = load_config(self.program_root)
        self.assertTrue(config.face_recognition.enabled)
        self.assertEqual(config.face_recognition.model_name, "buffalo_s")
        self.assertFalse(config.openclip.enabled)
        self.assertEqual(config.openclip.model_root, self.program_root / "models" / "openclip")
        self.assertEqual(config.openclip.device, "cpu")
        self.assertEqual(config.openclip.model_name, "ViT-L-14")
        self.assertEqual(config.openclip.pretrained, "laion2b_s32b_b82k")
        config_text = (self.program_root / "bildebank-config.toml").read_text(encoding="utf-8")
        self.assertIn("[image_search]", config_text)
        self.assertNotIn("[openclip]", config_text)

        code, stdout, stderr = capture_cli(["config", "image_search", "enable"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("image_search.enabled er satt til true.", stdout)
        self.assertTrue(load_config(self.program_root).openclip.enabled)

    def test_config_rejects_unknown_section_without_writing_file(self) -> None:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                main(["config", "unknown", "enable"])

        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("invalid choice", stderr.getvalue())
        self.assertFalse((self.program_root / "bildebank-config.toml").exists())

    def test_load_config_reads_local_face_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bildebank-config.toml").write_text(
                """
[face_recognition]
enabled = true
provider = "cpu"
model_root = "models/insightface"
database_dir = "faces"
model_name = "buffalo_s"

[openclip]
enabled = true
model_root = "models/openclip"
device = "cpu"
model_name = "ViT-L-14"
pretrained = "laion2b_s32b_b82k"
""",
                encoding="utf-8",
            )

            config = load_config(root)

            self.assertTrue(config.face_recognition.enabled)
            self.assertEqual(config.face_recognition.provider, "cpu")
            self.assertEqual(config.face_recognition.model_root, root / "models" / "insightface")
            self.assertEqual(config.face_recognition.database_dir, Path("faces"))
            self.assertEqual(config.face_recognition.model_name, "buffalo_s")
            self.assertTrue(config.openclip.enabled)
            self.assertEqual(config.openclip.model_root, root / "models" / "openclip")
            self.assertEqual(config.openclip.device, "cpu")
            self.assertEqual(config.openclip.model_name, "ViT-L-14")
            self.assertEqual(config.openclip.pretrained, "laion2b_s32b_b82k")
            config_text = (root / "bildebank-config.toml").read_text(encoding="utf-8")
            self.assertIn("[image_search]", config_text)
            self.assertNotIn("[openclip]", config_text)

    def test_load_config_reads_person_reference_links_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bildebank-config.toml").write_text(
                """
[browser]
person_reference_links_enabled = true
""",
                encoding="utf-8",
            )

            config = load_config(root)

        self.assertTrue(config.browser.person_reference_links_enabled)
        self.assertFalse(BrowserConfig().person_reference_links_enabled)

    def test_load_config_prefers_image_search_over_openclip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bildebank-config.toml").write_text(
                """
[openclip]
enabled = true
model_root = "models/openclip"
device = "cpu"
model_name = "ViT-L-14"
pretrained = "laion2b_s32b_b82k"

[image_search]
enabled = false
""",
                encoding="utf-8",
            )

            config = load_config(root)

            self.assertFalse(config.openclip.enabled)
            self.assertEqual(config.openclip.model_root, root / "models" / "openclip")
            self.assertEqual(config.openclip.device, "cpu")
            self.assertEqual(config.openclip.model_name, "ViT-L-14")
            self.assertEqual(config.openclip.pretrained, "laion2b_s32b_b82k")

