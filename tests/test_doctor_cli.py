from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.cli import build_parser
from bildebank.db import DB_FILENAME, init_database
from bildebank.media import sha256_file
from bildebank.ffmpeg_tools import FFmpegTools
from bildebank.openclip import OpenClipConfig, connect_openclip_db, embedding_blob, openclip_db_path
from tests.cli_helpers import capture_cli, run_cli
from tests.db_test_helpers import insert_test_file, register_target_file


class DoctorCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.program_root_tempdir = tempfile.TemporaryDirectory()
        self.program_root = Path(self.program_root_tempdir.name)
        self.program_root_patcher = patch("bildebank.cli.program_repo_root", return_value=self.program_root)
        self.program_root_patcher.start()

    def tearDown(self) -> None:
        self.program_root_patcher.stop()
        self.program_root_tempdir.cleanup()

    def test_doctor_is_disabled_by_default(self) -> None:
        exiftool = self.program_root / "bildebank-tools" / "exiftool" / "exiftool.exe"
        with (
            patch("bildebank.cli_doctor.resolve_exiftool_path", return_value=exiftool),
            patch("bildebank.cli_doctor.validate_exiftool_install", return_value="13.58"),
            patch("bildebank.cli_doctor.python_module_available", side_effect=lambda name: name == "h3"),
        ):
            code, stdout, stderr = capture_cli(["doctor"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("Bildebank doctor", stdout)
        self.assertIn("  OK: h3 installert", stdout)
        self.assertIn("  OK: ExifTool funnet:", stdout)
        self.assertIn("  OBS: face_recognition er slått av.", stdout)
        self.assertIn("  OBS: image_search er slått av.", stdout)
        self.assertIn("  OBS: ingen aktiv bildesamling funnet.", stdout)
        self.assertEqual(stderr, "")

    def test_doctor_deep_is_explicit(self) -> None:
        default_args = build_parser().parse_args(["doctor"])
        deep_args = build_parser().parse_args(["doctor", "--deep"])

        self.assertFalse(default_args.deep)
        self.assertTrue(deep_args.deep)

    def test_doctor_shows_exiftool_status(self) -> None:
        exiftool = self.program_root / "bildebank-tools" / "exiftool" / "exiftool.exe"
        with (
            patch("bildebank.cli_doctor.resolve_exiftool_path", return_value=exiftool),
            patch("bildebank.cli_doctor.validate_exiftool_install", return_value="13.58"),
        ):
            code, stdout, stderr = capture_cli(["doctor"])

        self.assertEqual(code, 0, stderr)
        self.assertIn(f"  OK: ExifTool funnet: {exiftool} (13.58)", stdout)

    def test_doctor_reports_missing_exiftool_without_failing(self) -> None:
        with patch("bildebank.cli_doctor.resolve_exiftool_path", side_effect=FileNotFoundError("mangler")):
            code, stdout, stderr = capture_cli(["doctor"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("  FEIL: ExifTool mangler eller virker ikke: mangler", stdout)
        self.assertIn("  Råd:", stdout)
        self.assertEqual(stderr, "")

    def test_doctor_reports_ffmpeg_status_without_failing(self) -> None:
        ffmpeg = self.program_root / "bildebank-tools" / "ffmpeg" / "8.1.2" / "bin" / "ffmpeg.exe"
        tools = FFmpegTools(ffmpeg, ffmpeg.with_name("ffprobe.exe"), "ffmpeg version 8.1.2", True)
        with patch("bildebank.cli_doctor.resolve_ffmpeg_tools", return_value=tools):
            code, stdout, stderr = capture_cli(["doctor"])

        self.assertEqual(code, 0, stderr)
        self.assertIn(f"  OK: FFmpeg funnet: {ffmpeg} (ffmpeg version 8.1.2)", stdout)

        with patch("bildebank.cli_doctor.resolve_ffmpeg_tools", side_effect=FileNotFoundError("mangler")):
            code, stdout, stderr = capture_cli(["doctor"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("  FEIL: FFmpeg mangler eller virker ikke: mangler", stdout)

    def test_doctor_reports_enabled_face_recognition_missing_dependencies(self) -> None:
        (self.program_root / "bildebank-config.toml").write_text(
            """
[face_recognition]
enabled = true
""",
            encoding="utf-8",
        )

        with (
            patch("bildebank.cli_doctor.resolve_exiftool_path", side_effect=FileNotFoundError("mangler")),
            patch("bildebank.cli_doctor.python_module_available", side_effect=lambda name: name == "h3"),
            patch(
                "bildebank.cli_doctor.insightface_runtime_error",
                return_value="InsightFace er ikke installert. Kjør install-insightface.ps1 fra programmappen.",
            ),
        ):
            code, stdout, stderr = capture_cli(["doctor"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("  OK: face_recognition er slått på", stdout)
        self.assertIn("  FEIL: InsightFace er ikke installert.", stdout)
        self.assertIn("  FEIL: face_recognition er slått på, men onnxruntime mangler.", stdout)
        self.assertIn("install-insightface.ps1", stdout)
        self.assertEqual(stderr, "")

    def test_doctor_reports_insightface_opencv_linux_system_dependency(self) -> None:
        (self.program_root / "bildebank-config.toml").write_text(
            """
[face_recognition]
enabled = true
""",
            encoding="utf-8",
        )

        with (
            patch("bildebank.cli_doctor.resolve_exiftool_path", side_effect=FileNotFoundError("mangler")),
            patch("bildebank.cli_doctor.python_module_available", side_effect=lambda name: name in {"h3", "onnxruntime"}),
            patch(
                "bildebank.cli_doctor.insightface_runtime_error",
                return_value=(
                    "InsightFace er installert, men OpenCV mangler Linux-biblioteket libGL.so.1. "
                    "Installer det i WSL/Linux med `sudo apt install libgl1`."
                ),
            ),
        ):
            code, stdout, stderr = capture_cli(["doctor"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("  FEIL: InsightFace er installert, men OpenCV mangler Linux-biblioteket libGL.so.1.", stdout)
        self.assertIn("  Råd: Installer Linux-pakken: `sudo apt install libgl1`.", stdout)
        self.assertIn("  OK: onnxruntime installert", stdout)
        self.assertEqual(stderr, "")

    def test_doctor_reports_enabled_image_search_missing_dependencies(self) -> None:
        (self.program_root / "bildebank-config.toml").write_text(
            """
[image_search]
enabled = true
""",
            encoding="utf-8",
        )

        with (
            patch("bildebank.cli_doctor.resolve_exiftool_path", side_effect=FileNotFoundError("mangler")),
            patch("bildebank.cli_doctor.python_module_available", side_effect=lambda name: name == "h3"),
            patch("bildebank.cli_doctor.torch_gpu_status", return_value={"torch": "nei", "cuda": "nei", "device": "-"}),
        ):
            code, stdout, stderr = capture_cli(["doctor"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("  OK: image_search er slått på", stdout)
        self.assertIn("  FEIL: image_search er slått på, men open_clip mangler.", stdout)
        self.assertIn("  FEIL: image_search er slått på, men torch mangler.", stdout)
        self.assertIn("install-openclip.ps1", stdout)
        self.assertEqual(stderr, "")

    def test_doctor_reports_missing_h3_without_failing(self) -> None:
        with (
            patch("bildebank.cli_doctor.resolve_exiftool_path", side_effect=FileNotFoundError("mangler")),
            patch("bildebank.cli_doctor.python_module_available", return_value=False),
        ):
            code, stdout, stderr = capture_cli(["doctor"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("  FEIL: h3 mangler. Geografiske funksjoner virker ikke.", stdout)
        self.assertIn("  Råd: Kjør setup-windows.ps1 på nytt", stdout)
        self.assertEqual(stderr, "")

    def test_doctor_reports_database_file_missing_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            conn = db.connect(target)
            try:
                conn.execute(
                    """
                    INSERT INTO files(
                        target_path, target_path_key, original_filename,
                        stored_filename, sha256, size_bytes, date_source
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "2024/01/missing.jpg",
                        db.relative_path_key(Path("2024/01/missing.jpg")),
                        "missing.jpg",
                        "missing.jpg",
                        "missing-file-sha256",
                        123,
                        "filename",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            with (
                patch("bildebank.cli_doctor.resolve_exiftool_path", side_effect=FileNotFoundError("mangler")),
                patch("bildebank.cli_doctor.python_module_available", return_value=False),
            ):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "doctor"]
                )

        self.assertEqual(code, 0, stderr)
        self.assertIn("Doctor filer: kontrollert=1/1", stdout)
        self.assertIn(
            "Doctor filer: ferdig kontrollert 1/1 filer.",
            stdout,
        )
        self.assertIn(
            "FEIL: 1 aktiv(e) databasefil(er) mangler på disk.",
            stdout,
        )
        self.assertIn(
            "INFO: file #1: 2024/01/missing.jpg",
            stdout,
        )
        self.assertIn("Undersøk filene og sikkerhetskopien", stdout)

    def test_doctor_reports_database_files_present_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            stored_path = target / "2024" / "01" / "present.jpg"
            stored_path.parent.mkdir(parents=True)
            stored_path.write_bytes(b"present")
            conn = db.connect(target)
            try:
                conn.execute(
                    """
                    INSERT INTO files(
                        target_path, target_path_key, original_filename,
                        stored_filename, sha256, size_bytes, date_source
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "2024/01/present.jpg",
                        db.relative_path_key(Path("2024/01/present.jpg")),
                        "present.jpg",
                        "present.jpg",
                        "present-file-sha256",
                        stored_path.stat().st_size,
                        "filename",
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO files(
                        target_path, target_path_key, original_filename,
                        stored_filename, sha256, size_bytes, date_source,
                        deleted_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (
                        "deleted/2024/01/missing.jpg",
                        db.relative_path_key(
                            Path("deleted/2024/01/missing.jpg")
                        ),
                        "missing.jpg",
                        "missing.jpg",
                        "deleted-missing-file-sha256",
                        123,
                        "filename",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            with (
                patch("bildebank.cli_doctor.resolve_exiftool_path", side_effect=FileNotFoundError("mangler")),
                patch("bildebank.cli_doctor.python_module_available", return_value=False),
                patch("bildebank.cli_doctor.sha256_file") as hash_file,
            ):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "doctor"]
                )

        self.assertEqual(code, 0, stderr)
        hash_file.assert_not_called()
        self.assertIn("Doctor filer: kontrollert=1/1", stdout)
        self.assertIn(
            "Doctor filer: ferdig kontrollert 1/1 filer.",
            stdout,
        )
        self.assertIn(
            "OK: alle 1 aktive databasefiler finnes på disk",
            stdout,
        )
        self.assertNotIn("aktiv(e) databasefil(er) mangler på disk", stdout)
        self.assertNotIn("Dyp filintegritet:", stdout)

    def test_doctor_deep_reports_missing_unreadable_and_wrong_hash(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            good_path = target / "2024" / "01" / "good.jpg"
            changed_path = target / "2024" / "01" / "changed.jpg"
            unreadable_path = target / "2024" / "01" / "unreadable.jpg"
            for path, content in (
                (good_path, b"good"),
                (changed_path, b"before"),
                (unreadable_path, b"unreadable"),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                register_target_file(target, path.relative_to(target))

            changed_path.write_bytes(b"after")
            conn = db.connect(target)
            try:
                conn.execute(
                    """
                    INSERT INTO files(
                        target_path, target_path_key, original_filename,
                        stored_filename, sha256, size_bytes, date_source
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "2024/01/missing.jpg",
                        db.relative_path_key(Path("2024/01/missing.jpg")),
                        "missing.jpg",
                        "missing.jpg",
                        "missing-file-sha256",
                        123,
                        "filename",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            database_path = target / DB_FILENAME
            database_before = database_path.read_bytes()

            def hash_or_fail(path: Path) -> str:
                if path.name == "unreadable.jpg":
                    raise OSError("ingen lesetilgang")
                return sha256_file(path)

            with (
                patch("bildebank.cli_doctor.resolve_exiftool_path", side_effect=FileNotFoundError("mangler")),
                patch("bildebank.cli_doctor.python_module_available", return_value=False),
                patch("bildebank.cli_doctor.sha256_file", side_effect=hash_or_fail),
            ):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "doctor", "--deep"]
                )

            database_after = database_path.read_bytes()

        self.assertEqual(code, 0, stderr)
        self.assertIn("Dyp filintegritet:", stdout)
        self.assertIn("Doctor SHA-256: kontrollert=1/4", stdout)
        self.assertIn("Doctor SHA-256: kontrollert=4/4", stdout)
        self.assertIn(
            "Doctor SHA-256: ferdig kontrollert 4/4 filer.",
            stdout,
        )
        self.assertIn("INFO: aktive databasefiler kontrollert: 4", stdout)
        self.assertIn("FEIL: 1 aktiv(e) fil(er) mangler på disk.", stdout)
        self.assertIn("FEIL: 1 aktiv(e) fil(er) kunne ikke leses.", stdout)
        self.assertIn("unreadable.jpg (ingen lesetilgang)", stdout)
        self.assertIn("FEIL: 1 aktiv(e) fil(er) har feil SHA-256.", stdout)
        self.assertIn("changed.jpg", stdout)
        self.assertEqual(database_after, database_before)

    def test_doctor_reports_orphan_file_in_collection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            registered_path = target / "2024" / "01" / "registered.jpg"
            registered_path.parent.mkdir(parents=True)
            registered_path.write_bytes(b"registered")
            register_target_file(
                target,
                registered_path.relative_to(target),
            )
            orphan_path = target / "2024" / "01" / "orphan.jpg"
            orphan_path.write_bytes(b"orphan")
            (target / "2024" / "01" / "notes.txt").write_text(
                "ikke media",
                encoding="utf-8",
            )

            with (
                patch("bildebank.cli_doctor.resolve_exiftool_path", side_effect=FileNotFoundError("mangler")),
                patch("bildebank.cli_doctor.python_module_available", return_value=False),
            ):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "doctor"]
                )

        self.assertEqual(code, 0, stderr)
        self.assertNotIn("Dyp filintegritet:", stdout)
        self.assertNotIn("Doctor SHA-256:", stdout)
        self.assertIn("Doctor orphan: scannet=1", stdout)
        self.assertIn(
            "Doctor orphan: ferdig scannet 2 mediefiler.",
            stdout,
        )
        self.assertIn(
            "FEIL: 1 orphan-fil(er) finnes i samlingen uten databasepost.",
            stdout,
        )
        self.assertIn("INFO: orphan: 2024/01/orphan.jpg", stdout)
        self.assertNotIn("orphan: 2024/01/registered.jpg", stdout)
        self.assertNotIn("orphan: 2024/01/notes.txt", stdout)

    def test_doctor_reports_orphan_openclip_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            active_id = insert_test_file(target, "2024/01/active.png", sha256="sha-active")
            deleted_id = insert_test_file(
                target,
                "deleted/2024/01/deleted.png",
                sha256="sha-deleted",
                deleted=True,
            )
            missing_id = active_id + deleted_id + 100
            OpenClipConfig(model_name="Test-Model", pretrained="test-weights")
            conn = connect_openclip_db(target)
            try:
                conn.executemany(
                    """
                    INSERT INTO image_embeddings(
                        file_id, target_path, target_path_key, sha256,
                        model_name, pretrained, embedding
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            active_id,
                            "2024/01/active.png",
                            "2024/01/active.png",
                            "sha-active",
                            "Test-Model",
                            "test-weights",
                            embedding_blob([1.0, 0.0]),
                        ),
                        (
                            deleted_id,
                            "deleted/2024/01/deleted.png",
                            "deleted/2024/01/deleted.png",
                            "sha-deleted",
                            "Test-Model",
                            "test-weights",
                            embedding_blob([0.0, 1.0]),
                        ),
                        (
                            missing_id,
                            "2026/01/unimported.png",
                            "2026/01/unimported.png",
                            "sha-missing",
                            "Test-Model",
                            "test-weights",
                            embedding_blob([0.5, 0.5]),
                        ),
                    ],
                )
                run_id = conn.execute(
                    """
                    INSERT INTO image_search_runs(query, model_name, pretrained, result_limit)
                    VALUES('cat', 'Test-Model', 'test-weights', 10)
                    """
                ).lastrowid
                conn.execute(
                    """
                    INSERT INTO image_search_results(
                        run_id, file_id, target_path, target_path_key, similarity, rank
                    ) VALUES(?, ?, '2026/01/unimported.png', '2026/01/unimported.png', 0.9, 1)
                    """,
                    (run_id, missing_id),
                )
                conn.commit()
            finally:
                conn.close()

            with (
                patch("bildebank.cli_doctor.resolve_exiftool_path", side_effect=FileNotFoundError("mangler")),
                patch("bildebank.cli_doctor.python_module_available", return_value=False),
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "doctor"])

        self.assertEqual(code, 0, stderr)
        self.assertIn(
            "FEIL: 2 OpenCLIP embedding-rad(er) peker på manglende eller slettet fil.",
            stdout,
        )
        self.assertIn(
            "INFO: image_embeddings file #",
            stdout,
        )
        self.assertIn("deleted/2024/01/deleted.png", stdout)
        self.assertIn("2026/01/unimported.png", stdout)
        self.assertIn(
            "FEIL: 1 OpenCLIP søkeresultat-rad(er) peker på manglende eller slettet fil.",
            stdout,
        )
        self.assertIn("image_search_results file #", stdout)
        self.assertIn("Råd: Kjør bildebank cleanup-image-search --apply", stdout)
        self.assertNotIn("image_embeddings file #1: 2024/01/active.png", stdout)

    def test_doctor_uses_explicit_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "doctor"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Aktiv bildesamling:", stdout)
            self.assertIn(str(target.resolve()), stdout)

    def test_doctor_does_not_migrate_openclip_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            old_target = root / "old-target"
            old_image = old_target / "2024" / "01" / "IMG_20240102.jpg"

            self.assertEqual(run_cli(["create", str(target)]), 0)
            conn = sqlite3.connect(openclip_db_path(target))
            try:
                conn.executescript(
                    """
                    CREATE TABLE meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE image_embeddings (
                        file_id INTEGER NOT NULL,
                        target_path TEXT NOT NULL,
                        target_path_key TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        model_name TEXT NOT NULL,
                        pretrained TEXT NOT NULL,
                        embedding BLOB NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(file_id, model_name, pretrained)
                    );
                    """
                )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('target_path', ?)",
                    (str(old_target),),
                )
                conn.execute(
                    """
                    INSERT INTO image_embeddings(
                        file_id, target_path, target_path_key, sha256, model_name, pretrained, embedding
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        1,
                        str(old_image),
                        str(old_image),
                        "sha",
                        "ViT-B-32",
                        "laion2b_s34b_b79k",
                        b"embedding",
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "doctor"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("bilde-embeddings: 1", stdout)
            conn = sqlite3.connect(openclip_db_path(target))
            try:
                self.assertEqual(
                    conn.execute("SELECT value FROM meta WHERE key = 'target_path'").fetchone()[0],
                    str(old_target),
                )
                self.assertEqual(
                    conn.execute("SELECT target_path FROM image_embeddings").fetchone()[0],
                    str(old_image),
                )
            finally:
                conn.close()
