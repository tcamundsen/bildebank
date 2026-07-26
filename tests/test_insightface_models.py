from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from bildebank import insightface_models
from bildebank.config import FaceRecognitionConfig
from bildebank.insightface_models import (
    InsightFaceModelFile,
    InsightFaceModelSpec,
)


TEST_FILES = (
    InsightFaceModelFile("detector.onnx", 8),
    InsightFaceModelFile("recognition.onnx", 11),
)


def write_model_archive(
    path: Path,
    *,
    prefix: str = "test/",
    unexpected: bool = False,
) -> InsightFaceModelSpec:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{prefix}detector.onnx", b"detector")
        archive.writestr(f"{prefix}recognition.onnx", b"recognition")
        if unexpected:
            archive.writestr(f"{prefix}unexpected.txt", b"unexpected")
    return InsightFaceModelSpec(
        name="test",
        archive_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        archive_size_bytes=path.stat().st_size,
        archive_prefix=prefix,
        files=TEST_FILES,
    )


class InsightFaceModelTests(unittest.TestCase):
    def test_official_model_archives_are_pinned(self) -> None:
        antelope = insightface_models.INSIGHTFACE_MODEL_SPECS["antelopev2"]
        buffalo = insightface_models.INSIGHTFACE_MODEL_SPECS["buffalo_l"]

        self.assertEqual(
            antelope.archive_sha256,
            "8e182f14fc6e80b3bfa375b33eb6cff7ee05d8ef7633e738d1c89021dcf0c5c5",
        )
        self.assertEqual(antelope.archive_size_bytes, 360_662_982)
        self.assertEqual(
            buffalo.archive_sha256,
            "80ffe37d8a5940d59a7384c201a2a38d4741f2f3c51eef46ebb28218a7b0ca2f",
        )
        self.assertEqual(buffalo.archive_size_bytes, 288_621_354)
        self.assertEqual(
            antelope.archive_url,
            "https://github.com/deepinsight/insightface/releases/download/v0.7/antelopev2.zip",
        )

    def test_known_model_requires_every_expected_file_with_correct_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = FaceRecognitionConfig(
                model_root=Path(tmp),
                model_name="test",
            )
            model_dir = insightface_models.insightface_model_dir(config)
            model_dir.mkdir(parents=True)
            (model_dir / "detector.onnx").write_bytes(b"detector")
            spec = InsightFaceModelSpec(
                name="test",
                archive_sha256="unused",
                archive_size_bytes=0,
                archive_prefix="test/",
                files=TEST_FILES,
            )

            with patch.object(
                insightface_models,
                "INSIGHTFACE_MODEL_SPECS",
                {"test": spec},
            ):
                self.assertFalse(
                    insightface_models.insightface_model_files_exist(config)
                )
                (model_dir / "recognition.onnx").write_bytes(b"recognition")
                self.assertTrue(
                    insightface_models.insightface_model_files_exist(config)
                )
                (model_dir / "recognition.onnx").write_bytes(b"wrong")
                self.assertFalse(
                    insightface_models.insightface_model_files_exist(config)
                )

    def test_install_downloads_validates_and_publishes_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "model.zip"
            spec = write_model_archive(archive)
            config = FaceRecognitionConfig(
                model_root=root / "models-root",
                model_name="test",
            )

            with (
                patch.object(
                    insightface_models,
                    "INSIGHTFACE_MODEL_SPECS",
                    {"test": spec},
                ),
                patch.object(
                    insightface_models,
                    "_download_file",
                    side_effect=lambda _url, path, **_kwargs: shutil.copyfile(archive, path),
                ),
            ):
                destination = insightface_models.install_insightface_model(config)

            self.assertEqual(destination, config.model_root / "models" / "test")
            self.assertEqual((destination / "detector.onnx").read_bytes(), b"detector")
            self.assertEqual(
                (destination / "recognition.onnx").read_bytes(),
                b"recognition",
            )
            self.assertFalse((destination / "test").exists())

    def test_wrong_hash_preserves_existing_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "model.zip"
            spec = replace(
                write_model_archive(archive),
                archive_sha256="0" * 64,
            )
            config = FaceRecognitionConfig(
                model_root=root / "models-root",
                model_name="test",
            )
            destination = insightface_models.insightface_model_dir(config)
            destination.mkdir(parents=True)

            with (
                patch.object(
                    insightface_models,
                    "INSIGHTFACE_MODEL_SPECS",
                    {"test": spec},
                ),
                patch.object(
                    insightface_models,
                    "_download_file",
                    side_effect=lambda _url, path, **_kwargs: shutil.copyfile(archive, path),
                ),
                self.assertRaisesRegex(ValueError, "feil SHA-256"),
            ):
                insightface_models.install_insightface_model(config)

            self.assertTrue(destination.is_dir())
            self.assertEqual(list(destination.iterdir()), [])

    def test_nonempty_incomplete_model_is_preserved_without_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = FaceRecognitionConfig(
                model_root=root / "models-root",
                model_name="test",
            )
            destination = insightface_models.insightface_model_dir(config)
            destination.mkdir(parents=True)
            existing = destination / "detector.onnx"
            existing.write_bytes(b"partial")
            spec = InsightFaceModelSpec(
                name="test",
                archive_sha256="unused",
                archive_size_bytes=0,
                archive_prefix="test/",
                files=TEST_FILES,
            )

            with (
                patch.object(
                    insightface_models,
                    "INSIGHTFACE_MODEL_SPECS",
                    {"test": spec},
                ),
                patch.object(insightface_models, "_download_file") as download,
                self.assertRaisesRegex(ValueError, "beholdes uendret"),
            ):
                insightface_models.install_insightface_model(config)

            self.assertEqual(existing.read_bytes(), b"partial")
            download.assert_not_called()

    def test_archive_with_unexpected_file_cleans_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "model.zip"
            spec = write_model_archive(archive, unexpected=True)
            config = FaceRecognitionConfig(
                model_root=root / "models-root",
                model_name="test",
            )
            destination = insightface_models.insightface_model_dir(config)

            with (
                patch.object(
                    insightface_models,
                    "INSIGHTFACE_MODEL_SPECS",
                    {"test": spec},
                ),
                patch.object(
                    insightface_models,
                    "_download_file",
                    side_effect=lambda _url, path, **_kwargs: shutil.copyfile(archive, path),
                ),
                self.assertRaisesRegex(ValueError, "uventet fil"),
            ):
                insightface_models.install_insightface_model(config)

            self.assertFalse(destination.exists())
            self.assertEqual(
                list(destination.parent.glob(".test.installing-*")),
                [],
            )

    def test_install_rolls_back_on_interrupt_during_publication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "model.zip"
            spec = write_model_archive(archive)
            config = FaceRecognitionConfig(
                model_root=root / "models-root",
                model_name="test",
            )
            destination = insightface_models.insightface_model_dir(config)
            destination.mkdir(parents=True)
            original_rename = Path.rename

            def interrupt_staging_publication(path: Path, target: Path) -> Path:
                if (
                    path.name.startswith(".test.installing-")
                    and Path(target) == destination
                ):
                    raise KeyboardInterrupt
                return original_rename(path, target)

            with (
                patch.object(
                    insightface_models,
                    "INSIGHTFACE_MODEL_SPECS",
                    {"test": spec},
                ),
                patch.object(
                    insightface_models,
                    "_download_file",
                    side_effect=lambda _url, path, **_kwargs: shutil.copyfile(archive, path),
                ),
                patch.object(
                    Path,
                    "rename",
                    autospec=True,
                    side_effect=interrupt_staging_publication,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                insightface_models.install_insightface_model(config)

            self.assertTrue(destination.is_dir())
            self.assertEqual(list(destination.iterdir()), [])
            self.assertEqual(
                list(destination.parent.glob(".test.installing-*")),
                [],
            )
            self.assertEqual(
                list(destination.parent.glob(".test.previous-*")),
                [],
            )

    def test_unknown_model_is_not_downloaded_without_pinned_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = FaceRecognitionConfig(
                model_root=Path(tmp),
                model_name="unknown",
            )

            with (
                patch.object(insightface_models, "_download_file") as download,
                self.assertRaisesRegex(ValueError, "ukjent InsightFace-modell"),
            ):
                insightface_models.install_insightface_model(config)

            download.assert_not_called()

    def test_install_rejects_linked_model_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            linked = root / "linked"
            try:
                linked.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Kan ikke opprette symlink: {exc}")
            spec = InsightFaceModelSpec(
                name="test",
                archive_sha256="0" * 64,
                archive_size_bytes=1,
                archive_prefix="",
                files=TEST_FILES,
            )
            config = FaceRecognitionConfig(
                model_root=linked,
                model_name="test",
            )

            with (
                patch.object(
                    insightface_models,
                    "INSIGHTFACE_MODEL_SPECS",
                    {"test": spec},
                ),
                patch.object(insightface_models, "_download_file") as download,
                self.assertRaisesRegex(ValueError, "lenker"),
            ):
                insightface_models.install_insightface_model(config)

            download.assert_not_called()
            self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
