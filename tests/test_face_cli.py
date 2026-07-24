from __future__ import annotations

import sqlite3
import struct
import sys
import tempfile
import unittest
import warnings
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.cli import main
from bildebank.config import FaceRecognitionConfig, load_config
from bildebank.db import DB_FILENAME
from bildebank.face import (
    apply_face_schema,
    connect_face_db,
    delete_face_database,
    face_box_percent,
    face_db_path,
    insightface_import_error_message,
    normalize_insightface_model_layout,
    read_image,
    remove_insightface_model_zip,
    scan_faces,
)
from bildebank.media import ImageDimensions
from bildebank.openclip import embedding_blob
from bildebank.server_faces import cached_face_box_media_metadata, update_face_box_media_metadata
from bildebank.target_lock import LOCK_FILENAME, TargetLock, TargetLockError
from tests.cli_helpers import capture_cli, run_cli
from tests.test_media import minimal_png


def face_database_dump(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


def create_face_v4_database(target: Path, *, partial_v5: bool = False) -> Path:
    path = face_db_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        apply_face_schema(conn)
        conn.execute(
            """
            INSERT INTO scanned_files(
                file_id, target_path, target_path_key, sha256, status, face_count
            ) VALUES(1, 'image.jpg', 'image.jpg', 'hash', 'ok', 1)
            """
        )
        conn.execute(
            """
            INSERT INTO faces(
                id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                detection_score, embedding_model, embedding
            ) VALUES(1, 1, 'image.jpg', 1, 2, 10, 20, 0.9, 'test', x'00000000')
            """
        )
        conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
        conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)")
        conn.execute("INSERT INTO person_files(person_id, file_id) VALUES(1, 1)")
        conn.execute(
            """
            INSERT INTO face_suggestions(person_id, face_id, similarity)
            VALUES(1, 10, 0.95)
            """
        )
        if not partial_v5:
            conn.execute("DROP INDEX idx_face_suggestions_reference_face_id")
            conn.execute(
                "ALTER TABLE face_suggestions DROP COLUMN reference_face_id"
            )
        conn.execute(
            "UPDATE meta SET value = '4' WHERE key = 'schema_version'"
        )
        conn.commit()
    finally:
        conn.close()
    return path


def connect_raw_face_db(
    target: Path,
    config: FaceRecognitionConfig | None = None,
) -> sqlite3.Connection:
    path = face_db_path(target, config)
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path)


class FaceCliTests(unittest.TestCase):

    def setUp(self) -> None:
        self.program_root_tempdir = tempfile.TemporaryDirectory()
        self.program_root = Path(self.program_root_tempdir.name)
        self.program_root_patcher = patch("bildebank.cli.program_repo_root", return_value=self.program_root)
        self.program_root_patcher.start()

    def tearDown(self) -> None:
        self.program_root_patcher.stop()
        self.program_root_tempdir.cleanup()

    def enable_face_recognition_config(self) -> None:
        (self.program_root / "bildebank-config.toml").write_text(
            """
[face_recognition]
enabled = true
provider = "cpu"
model_root = ".bildebank-insightface"
model_name = "buffalo_l"
""",
            encoding="utf-8",
        )

    def test_face_reset_help_documents_reset_levels(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["face-reset", "-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        normalized_stdout = " ".join(stdout.split())
        self.assertIn("--all", stdout)
        self.assertIn("--keep-scan", stdout)
        self.assertIn("Standard hvis ingen nivåvalg er brukt", normalized_stdout)
        self.assertIn("krever alltid", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_face_suggest_help_documents_threshold_and_model(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["face-suggest", "-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("--threshold", stdout)
        self.assertIn("--model", stdout)
        self.assertNotIn("--no-browser", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_face_person_rename_help_documents_names(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["face-person-rename", "-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("gammelt_navn", stdout)
        self.assertIn("nytt_navn", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_face_box_media_metadata_write_requires_target_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            (target / LOCK_FILENAME).write_text("command=remove\n", encoding="utf-8")

            with self.assertRaises(TargetLockError):
                update_face_box_media_metadata(
                    target,
                    1,
                    ImageDimensions(100, 80),
                    1,
                    123,
                )

            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                row = conn.execute(
                    """
                    SELECT media_width, media_height, media_orientation, media_metadata_mtime_ns
                    FROM files
                    """
                ).fetchone()
            finally:
                conn.close()

            self.assertEqual(row, (None, None, None, None))

    def test_face_box_media_metadata_cache_locks_before_reading_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            conn = db.connect(target)
            try:
                item = dict(
                    conn.execute(
                        """
                        SELECT
                            id, target_path, media_width, media_height,
                            media_orientation, media_metadata_mtime_ns
                        FROM files
                        """
                    ).fetchone()
                )
            finally:
                conn.close()
            (target / LOCK_FILENAME).write_text("command=remove\n", encoding="utf-8")

            with (
                patch(
                    "bildebank.server_faces.image_dimensions",
                    side_effect=AssertionError("filen skal ikke leses uten lås"),
                ),
                self.assertRaises(TargetLockError),
            ):
                cached_face_box_media_metadata(target, item)

    def test_face_box_media_metadata_read_only_does_not_fill_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            conn = db.connect(target)
            try:
                item = dict(conn.execute("SELECT * FROM files").fetchone())
            finally:
                conn.close()
            (target / LOCK_FILENAME).write_text("command=face-scan\n", encoding="utf-8")

            with patch(
                "bildebank.server_faces.image_dimensions",
                side_effect=AssertionError("read-only skal ikke fylle metadata-cache"),
            ):
                dimensions, orientation = cached_face_box_media_metadata(
                    target,
                    item,
                    write_metadata_cache=False,
                )

        self.assertIsNone(dimensions)
        self.assertEqual(orientation, 1)

    def test_face_scan_requires_enabled_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "face-scan", "--limit", "1"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Ansiktsgjenkjenning er av", stderr)
            self.assertFalse((face_db_path(target)).exists())

            code, stdout, stderr = capture_cli(["--target", str(target), "face-person-list"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Ansiktsgjenkjenning er av", stderr)
            self.assertFalse((face_db_path(target)).exists())

    def test_download_face_model_loads_selected_model_without_target_or_enabled_config(self) -> None:
        (self.program_root / "bildebank-config.toml").write_text(
            """
[face_recognition]
enabled = false
provider = "cpu"
model_root = ".bildebank-insightface"
model_name = "buffalo_l"
""",
            encoding="utf-8",
        )

        def fake_load_face_app(config):
            model_dir = config.model_root / "models" / config.model_name
            model_dir.mkdir(parents=True)
            (model_dir / "det_10g.onnx").write_bytes(b"model")
            return object()

        with patch("bildebank.face.load_face_app", side_effect=fake_load_face_app) as load_app:
            code, stdout, stderr = capture_cli(["download-face-model"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("InsightFace-modell: buffalo_l", stdout)
        self.assertIn("Modellen er lastet ned.", stdout)
        self.assertEqual(stderr, "")
        self.assertEqual(load_app.call_args.args[0].model_name, "buffalo_l")

    def test_face_scan_reports_insightface_opencv_linux_system_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")
            self.enable_face_recognition_config()

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            with patch(
                "bildebank.face.load_face_app",
                side_effect=ValueError(
                    "InsightFace er installert, men OpenCV mangler Linux-biblioteket libGL.so.1. "
                    "Installer det i WSL/Linux med `sudo apt install libgl1`."
                ),
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "face-scan", "--limit", "1"])

            self.assertEqual(code, 1)
            self.assertIn("Face-scan: laster ansiktsmodell.", stdout)
            self.assertNotIn("Oppsummering:", stdout)
            self.assertIn("Feil: InsightFace er installert, men OpenCV mangler Linux-biblioteket libGL.so.1.", stderr)
            self.assertIn("sudo apt install libgl1", stderr)

    def test_face_scan_and_suggest_refuse_to_run_while_target_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / LOCK_FILENAME).write_text("command=import\n", encoding="utf-8")

            scan_code, _scan_stdout, scan_stderr = capture_cli(
                ["--target", str(target), "face-scan", "--limit", "1"]
            )
            suggest_code, _suggest_stdout, suggest_stderr = capture_cli(
                ["--target", str(target), "face-suggest"]
            )

        self.assertEqual(scan_code, 1)
        self.assertIn("Bildesamlingen er låst", scan_stderr)
        self.assertEqual(suggest_code, 1)
        self.assertIn("Bildesamlingen er låst", suggest_stderr)

    def test_face_scan_releases_target_lock_while_insightface_processes_image(self) -> None:
        class FakeFace:
            bbox = [1.0, 2.0, 11.0, 22.0]
            det_score = 0.9
            embedding = [0.1, 0.2, 0.3]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")
            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            class FakeApp:
                def get(self, _image):
                    with TargetLock(target, command="concurrent-operation"):
                        pass
                    return [FakeFace()]

            with (
                patch("bildebank.face.load_face_app", return_value=FakeApp()),
                patch("bildebank.face.read_image", return_value=object()),
            ):
                stats = scan_faces(target, load_config(self.program_root).face_recognition)

        self.assertEqual(stats.scanned, 1)
        self.assertEqual(stats.errors, 0)

    def test_face_scan_discards_result_when_file_is_removed_during_processing(self) -> None:
        class FakeFace:
            bbox = [1.0, 2.0, 11.0, 22.0]
            det_score = 0.9
            embedding = [0.1, 0.2, 0.3]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")
            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            class FakeApp:
                def get(self, _image):
                    with TargetLock(target, command="concurrent-remove"):
                        conn = db.connect(target)
                        try:
                            conn.execute("UPDATE files SET deleted_at = CURRENT_TIMESTAMP WHERE id = 1")
                            conn.commit()
                        finally:
                            conn.close()
                    return [FakeFace()]

            config = load_config(self.program_root).face_recognition
            with (
                patch("bildebank.face.load_face_app", return_value=FakeApp()),
                patch("bildebank.face.read_image", return_value=object()),
            ):
                stats = scan_faces(target, config)
            conn = connect_face_db(target, config)
            try:
                scanned_rows = conn.execute("SELECT COUNT(*) FROM scanned_files").fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(stats.scanned, 0)
        self.assertEqual(stats.skipped, 1)
        self.assertEqual(scanned_rows, 0)

    def test_face_scan_does_not_restore_results_after_face_reset_all(self) -> None:
        class FakeFace:
            bbox = [1.0, 2.0, 11.0, 22.0]
            det_score = 0.9
            embedding = [0.1, 0.2, 0.3]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")
            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            config = load_config(self.program_root).face_recognition

            class FakeApp:
                def get(self, _image):
                    delete_face_database(target, config)
                    return [FakeFace()]

            with (
                patch("bildebank.face.load_face_app", return_value=FakeApp()),
                patch("bildebank.face.read_image", return_value=object()),
            ):
                stats = scan_faces(target, config)
            conn = connect_face_db(target, config)
            try:
                scanned_rows = conn.execute("SELECT COUNT(*) FROM scanned_files").fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(stats.scanned, 0)
        self.assertEqual(stats.skipped, 1)
        self.assertEqual(scanned_rows, 0)

    def test_face_scan_writes_faces_to_separate_database(self) -> None:
        class FakeFace:
            bbox = [1.0, 2.0, 11.0, 22.0]
            det_score = 0.9
            embedding = [0.1, 0.2, 0.3]

        class FakeApp:
            def get(self, image):
                print("internal model get stdout")
                warnings.warn("internal model warning", FutureWarning, stacklevel=1)
                return [FakeFace()]

        def fake_load_face_app(config):
            print("internal model load stdout")
            print("internal model load stderr", file=sys.stderr)
            return FakeApp()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")
            self.enable_face_recognition_config()

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )

            with (
                patch("bildebank.face.load_face_app", side_effect=fake_load_face_app),
                patch("bildebank.face.read_image", return_value=object()),
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "face-scan", "--limit", "1"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Face-scan: 1 bildefiler skal kontrolleres.", stdout)
            self.assertIn("Face-scan: 1 nye eller endrede bilder skal scannes.", stdout)
            self.assertIn("Face-scan: ansiktsmodellen finnes ikke lokalt.", stdout)
            self.assertIn("Face-scan: scannet=1/1", stdout)
            self.assertIn("gjenstår=0s", stdout)
            self.assertNotIn("internal model", stdout)
            self.assertNotIn("internal model", stderr)
            self.assertIn("ansikter=1", stdout)
            face_db = face_db_path(target, load_config(self.program_root).face_recognition)
            self.assertTrue(face_db.exists())
            conn = sqlite3.connect(face_db)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM scanned_files").fetchone()[0], 1)
                face = conn.execute(
                    "SELECT bbox_x, bbox_y, bbox_width, bbox_height, detection_score, embedding_model FROM faces"
                ).fetchone()
                self.assertEqual(face, (1.0, 2.0, 10.0, 20.0, 0.9, "buffalo_l"))
            finally:
                conn.close()

            with (
                patch("bildebank.face.load_face_app", side_effect=AssertionError("should not load model")),
                patch("bildebank.face.read_image", side_effect=AssertionError("should not read image")),
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "face-scan", "--limit", "1"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Face-scan: kontrollert=1/1", stdout)
            self.assertIn("hoppet_over=1", stdout)
            self.assertIn("scannet=0", stdout)

            code, stdout, stderr = capture_cli(["--target", str(target), "face-report"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Ansiktsrapport", stdout)
            self.assertIn("Scannede filer: 1", stdout)
            self.assertIn("Ansikter funnet: 1", stdout)
            self.assertIn("Filer med ett ansikt: 1", stdout)
            self.assertIn("Flest ansikter:", stdout)
            self.assertIn("Personstatus:", stdout)
            self.assertIn("Personer registrert: 0", stdout)
            self.assertIn("Bilder med ansikter, men ingen bekreftet person: 1", stdout)
            self.assertIn("IMG_20240102.jpg", stdout)

    def test_face_scan_prints_file_path_when_image_fails(self) -> None:
        class FakeApp:
            def get(self, image):
                return []

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            bad_image = source / "bad.jpg"
            bad_image.write_bytes(b"image")
            self.enable_face_recognition_config()

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )

            with (
                patch("bildebank.face.load_face_app", return_value=FakeApp()),
                patch("bildebank.face.read_image", side_effect=ValueError("Kunne ikke lese testbildet")),
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "face-scan", "--limit", "1"])

            self.assertEqual(code, 2)
            self.assertIn("Face-scan-feil:", stdout)
            self.assertIn("bad.jpg", stdout)
            self.assertIn("Kunne ikke lese testbildet", stdout)
            self.assertIn("feil=1", stdout)

            code, stdout, stderr = capture_cli(["--target", str(target), "face-report"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Siste scan-feil:", stdout)
            self.assertIn("bad.jpg", stdout)
            self.assertIn("Kunne ikke lese testbildet", stdout)

    def test_face_scan_force_rescans_limited_existing_files(self) -> None:
        class FakeFace:
            det_score = 0.9
            embedding = [0.1, 0.2, 0.3]

            def __init__(self, bbox):
                self.bbox = bbox

        class FakeApp:
            def __init__(self, face):
                self.face = face

            def get(self, image):
                return [self.face]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")
            self.enable_face_recognition_config()

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )

            with (
                patch("bildebank.face.load_face_app", return_value=FakeApp(FakeFace([1.0, 2.0, 11.0, 22.0]))),
                patch("bildebank.face.read_image", return_value=object()),
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "face-scan", "--limit", "1"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("scannet=1", stdout)

            face_db = face_db_path(target, load_config(self.program_root).face_recognition)
            conn = sqlite3.connect(face_db)
            conn.row_factory = sqlite3.Row
            try:
                old_face_id = int(conn.execute("SELECT id FROM faces").fetchone()["id"])
                person_id = int(
                    conn.execute("INSERT INTO persons(name) VALUES('Kari') RETURNING id").fetchone()["id"]
                )
                conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(?, ?)", (person_id, old_face_id))
                conn.execute(
                    "INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(?, ?, 0.8)",
                    (person_id, old_face_id),
                )
                conn.commit()
            finally:
                conn.close()

            with (
                patch("bildebank.face.load_face_app", return_value=FakeApp(FakeFace([3.0, 4.0, 13.0, 24.0]))),
                patch("bildebank.face.read_image", return_value=object()),
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "face-scan", "--force", "--limit", "1"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("hoppet_over=0", stdout)
            self.assertIn("scannet=1", stdout)

            conn = sqlite3.connect(face_db)
            try:
                face = conn.execute("SELECT id, bbox_x, bbox_y, bbox_width, bbox_height FROM faces").fetchone()
                self.assertNotEqual(face[0], old_face_id)
                self.assertEqual(face[1:], (3.0, 4.0, 10.0, 20.0))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM person_faces").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM face_suggestions").fetchone()[0], 0)
            finally:
                conn.close()

    def test_face_report_prints_relative_face_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            image_path = target / "2024" / "01" / "IMG_20240102.jpg"
            relative_image_path = Path("2024/01/IMG_20240102.jpg")

            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"image")
            config = load_config(self.program_root).face_recognition

            conn = connect_raw_face_db(target, config)
            try:
                apply_face_schema(conn)
                conn.execute(
                    """
                    INSERT INTO scanned_files(file_id, target_path, target_path_key, sha256, status, face_count)
                    VALUES(1, ?, ?, 'sha', 'ok', 1)
                    """,
                    (relative_image_path.as_posix(), db.relative_path_key(relative_image_path)),
                )
                conn.execute(
                    """
                    INSERT INTO faces(
                        file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    ) VALUES(1, ?, 1, 2, 3, 4, 0.9, 'test', ?)
                    """,
                    (db.relative_path_key(relative_image_path), b"embedding"),
                )
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "face-report"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("1\t2024/01/IMG_20240102.jpg", stdout)

    def test_face_database_rejects_absolute_target_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            image_path = target / "2024" / "01" / "IMG_20240102.jpg"
            target.mkdir()
            conn = connect_raw_face_db(target)
            try:
                apply_face_schema(conn)
                conn.execute(
                    """
                    INSERT INTO scanned_files(file_id, target_path, target_path_key, sha256, status, face_count)
                    VALUES(1, ?, '2024/01/img_20240102.jpg', 'sha', 'ok', 0)
                    """,
                    (str(image_path),),
                )
                conn.commit()
            finally:
                conn.close()

            with self.assertRaisesRegex(ValueError, "Face-databasen har absolutt target_path"):
                connect_face_db(target).close()

    def test_face_database_path_uses_model_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            config = FaceRecognitionConfig(model_name="antelopev2")

            conn = connect_face_db(target, config)
            try:
                self.assertEqual(face_db_path(target, config), target / ".bildebank-faces" / "antelopev2.sqlite3")
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'model_name'").fetchone()[0], "antelopev2")
            finally:
                conn.close()

    def test_face_database_rejects_model_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            antelope_config = FaceRecognitionConfig(model_name="antelopev2")
            conn = connect_raw_face_db(target, antelope_config)
            try:
                apply_face_schema(conn)
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('model_name', 'buffalo_l')"
                )
                conn.commit()
            finally:
                conn.close()

            with self.assertRaisesRegex(ValueError, "tilhører en annen modell"):
                connect_face_db(target, antelope_config).close()

    def test_face_database_moves_legacy_buffalo_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            legacy_path = target / ".bilder-faces.sqlite3"
            conn = sqlite3.connect(legacy_path)
            try:
                apply_face_schema(conn)
                conn.commit()
            finally:
                conn.close()

            config = FaceRecognitionConfig(model_name="buffalo_l")
            new_path = face_db_path(target, config)

            self.assertTrue(legacy_path.exists())
            self.assertFalse(new_path.exists())

            connect_face_db(target, config).close()

            self.assertFalse(legacy_path.exists())
            self.assertTrue(new_path.exists())

    def test_normalize_insightface_model_layout_moves_nested_onnx_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / ".bildebank-insightface" / "models" / "antelopev2" / "antelopev2"
            nested.mkdir(parents=True)
            (nested / "scrfd_10g_bnkps.onnx").write_bytes(b"detector")
            (nested / "glintr100.onnx").write_bytes(b"recognition")
            config = FaceRecognitionConfig(model_root=root / ".bildebank-insightface", model_name="antelopev2")

            self.assertTrue(normalize_insightface_model_layout(config))

            model_dir = root / ".bildebank-insightface" / "models" / "antelopev2"
            self.assertTrue((model_dir / "scrfd_10g_bnkps.onnx").exists())
            self.assertTrue((model_dir / "glintr100.onnx").exists())
            self.assertFalse(nested.exists())

    def test_remove_insightface_model_zip_removes_only_active_model_zip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            models_dir = root / ".bildebank-insightface" / "models"
            models_dir.mkdir(parents=True)
            active_zip = models_dir / "antelopev2.zip"
            other_zip = models_dir / "buffalo_l.zip"
            active_zip.write_bytes(b"zip")
            other_zip.write_bytes(b"zip")
            config = FaceRecognitionConfig(model_root=root / ".bildebank-insightface", model_name="antelopev2")

            self.assertTrue(remove_insightface_model_zip(config))
            self.assertFalse(active_zip.exists())
            self.assertTrue(other_zip.exists())
            self.assertFalse(remove_insightface_model_zip(config))

    def test_insightface_import_error_message_reports_linux_libgl_dependency(self) -> None:
        message = insightface_import_error_message(
            ImportError("libGL.so.1: cannot open shared object file: No such file or directory")
        )

        self.assertIn("OpenCV mangler Linux-biblioteket libGL.so.1", message)
        self.assertIn("sudo apt install libgl1", message)

    def test_face_suggest_uses_relative_face_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "new-name"
            image_path = target / "2021" / "08" / "2019-1-6-1.jpg"
            relative_image_path = Path("2021/08/2019-1-6-1.jpg")

            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(minimal_png(640, 480))
            config = load_config(self.program_root).face_recognition

            conn = connect_raw_face_db(target, config)
            try:
                apply_face_schema(conn)
                embedding = struct.pack("ff", 1.0, 0.0)
                conn.execute(
                    """
                    INSERT INTO scanned_files(file_id, target_path, target_path_key, sha256, status, face_count)
                    VALUES(1, ?, ?, 'sha', 'ok', 2)
                    """,
                    (relative_image_path.as_posix(), db.relative_path_key(relative_image_path)),
                )
                for face_id in (1, 2):
                    conn.execute(
                        """
                        INSERT INTO faces(
                            id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                            detection_score, embedding_model, embedding
                        ) VALUES(?, 1, ?, 1, 2, 30, 40, 0.9, 'test', ?)
                        """,
                        (face_id, db.relative_path_key(relative_image_path), embedding),
                    )
                conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)")
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "face-suggest"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Face-suggest: leser 1 bekreftede ansikter.", stdout)
            self.assertIn("Face-suggest: leser 1 ukjente ansikter.", stdout)
            self.assertIn("Face-suggest: sammenlignet=1/1", stdout)
            self.assertIn("forslag=1", stdout)
            self.assertNotIn("Kari\tface-id=2", stdout)
            conn = connect_raw_face_db(target, config)
            try:
                self.assertEqual(
                    conn.execute("SELECT target_path FROM scanned_files WHERE file_id = 1").fetchone()[0],
                    "2021/08/2019-1-6-1.jpg",
                )
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM face_suggestions").fetchone()[0], 1)
            finally:
                conn.close()

    def test_face_suggest_model_uses_model_specific_database_without_changing_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            relative_image_path = Path("2021/08/IMG_20210801.jpg")
            embedding = embedding_blob([1.0, 0.0, 0.0])
            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / relative_image_path).parent.mkdir(parents=True)
            (target / relative_image_path).write_bytes(minimal_png(640, 480))

            antelope_config = FaceRecognitionConfig(model_name="antelopev2")
            conn = connect_face_db(target, antelope_config)
            try:
                conn.execute(
                    """
                    INSERT INTO scanned_files(file_id, target_path, target_path_key, sha256, status, face_count)
                    VALUES(1, ?, ?, 'sha', 'ok', 2)
                    """,
                    (relative_image_path.as_posix(), db.relative_path_key(relative_image_path)),
                )
                for face_id in (1, 2):
                    conn.execute(
                        """
                        INSERT INTO faces(
                            id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                            detection_score, embedding_model, embedding
                        ) VALUES(?, 1, ?, 1, 2, 30, 40, 0.9, 'antelopev2', ?)
                        """,
                        (face_id, db.relative_path_key(relative_image_path), embedding),
                    )
                conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)")
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "face-suggest", "--model", "antelopev2"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Modell: antelopev2", stdout)
            self.assertIn("forslag=1", stdout)
            self.assertEqual(load_config(self.program_root).face_recognition.model_name, "buffalo_l")
            conn = connect_raw_face_db(target, antelope_config)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM face_suggestions").fetchone()[0], 1)
            finally:
                conn.close()
            buffalo_config = load_config(self.program_root).face_recognition
            self.assertFalse(face_db_path(target, buffalo_config).exists())

    def test_face_suggest_without_confirmed_faces_deletes_old_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            relative_image_path = Path("2021/08/IMG_20210801.jpg")
            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)

            config = load_config(self.program_root).face_recognition
            conn = connect_face_db(target, config)
            try:
                conn.execute(
                    """
                    INSERT INTO scanned_files(file_id, target_path, target_path_key, sha256, status, face_count)
                    VALUES(1, ?, ?, 'sha', 'ok', 1)
                    """,
                    (relative_image_path.as_posix(), db.relative_path_key(relative_image_path)),
                )
                conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    ) VALUES(1, 1, ?, 1, 2, 30, 40, 0.9, 'buffalo_l', ?)
                    """,
                    (db.relative_path_key(relative_image_path), embedding_blob([1.0, 0.0, 0.0])),
                )
                conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                conn.execute("INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 1, 0.95)")
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "face-suggest"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("personer=0", stdout)
            self.assertIn("ukjente_ansikter=1", stdout)
            self.assertIn("forslag=0", stdout)
            conn = connect_face_db(target, config)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM face_suggestions").fetchone()[0], 0)
            finally:
                conn.close()

    def test_face_suggest_matches_best_confirmed_face_not_person_centroid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            relative_image_path = Path("2021/08/IMG_20210801.jpg")
            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)

            config = load_config(self.program_root).face_recognition
            conn = connect_face_db(target, config)
            try:
                conn.execute(
                    """
                    INSERT INTO scanned_files(file_id, target_path, target_path_key, sha256, status, face_count)
                    VALUES(1, ?, ?, 'sha', 'ok', 4)
                    """,
                    (relative_image_path.as_posix(), db.relative_path_key(relative_image_path)),
                )
                faces = (
                    (1, [1.0, 0.0, 0.0]),
                    (2, [0.0, 1.0, 0.0]),
                    (3, [1.0, 0.0, 0.0]),
                    (4, [0.0, 0.0, 1.0]),
                )
                for face_id, embedding in faces:
                    conn.execute(
                        """
                        INSERT INTO faces(
                            id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                            detection_score, embedding_model, embedding
                        ) VALUES(?, 1, ?, 1, 2, 30, 40, 0.9, 'buffalo_l', ?)
                        """,
                        (face_id, db.relative_path_key(relative_image_path), embedding_blob(embedding)),
                    )
                conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                conn.execute("INSERT INTO persons(id, name) VALUES(2, 'Ola')")
                conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)")
                conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 2)")
                conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(2, 4)")
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "face-suggest", "--threshold", "0.9"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("personer=2", stdout)
            self.assertIn("ukjente_ansikter=1", stdout)
            self.assertIn("forslag=1", stdout)
            conn = connect_face_db(target, config)
            try:
                suggestion = conn.execute(
                    """
                    SELECT
                        persons.name,
                        face_suggestions.face_id,
                        face_suggestions.reference_face_id,
                        face_suggestions.similarity
                    FROM face_suggestions
                    JOIN persons ON persons.id = face_suggestions.person_id
                    """
                ).fetchone()
                self.assertIsNotNone(suggestion)
                self.assertEqual((suggestion[0], suggestion[1], suggestion[2]), ("Kari", 3, 1))
                self.assertAlmostEqual(suggestion[3], 1.0)
            finally:
                conn.close()

    def test_read_image_uses_unicode_safe_file_reading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "utenrødeøyne.jpg"
            path.write_bytes(b"image-bytes")

            class FakeData:
                size = 11

            class FakeNp:
                uint8 = object()

                @staticmethod
                def fromfile(filename, dtype):
                    self.assertEqual(filename, str(path))
                    self.assertIs(dtype, FakeNp.uint8)
                    return FakeData()

            class FakeCv2:
                IMREAD_COLOR = 1
                IMREAD_IGNORE_ORIENTATION = 128

                @staticmethod
                def imdecode(data, flags):
                    self.assertIsInstance(data, FakeData)
                    self.assertEqual(flags, FakeCv2.IMREAD_COLOR | FakeCv2.IMREAD_IGNORE_ORIENTATION)
                    return {"decoded": True}

            modules = {"cv2": FakeCv2, "numpy": FakeNp}
            with patch.dict(sys.modules, modules):
                self.assertEqual(read_image(path), {"decoded": True})

    def test_face_box_percent_accounts_for_exif_rotation(self) -> None:
        face = {"x": 10.0, "y": 20.0, "width": 30.0, "height": 40.0}
        dimensions = ImageDimensions(width=100, height=200)

        self.assertEqual(
            face_box_percent(face, dimensions, orientation=6),
            (70.0, 10.0, 20.0, 30.0),
        )

    def test_face_report_handles_missing_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "face-report"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Face-database finnes ikke.", stdout)
            self.assertIn("Kjør bildebank face-scan først.", stdout)

    def test_face_person_add_remove_face_and_suggest(self) -> None:
        class FakeFace:
            def __init__(self, bbox, embedding):
                self.bbox = bbox
                self.det_score = 0.9
                self.embedding = embedding

        class FakeApp:
            def get(self, image):
                return [
                    FakeFace([1.0, 2.0, 11.0, 22.0], [1.0, 0.0, 0.0]),
                    FakeFace([30.0, 4.0, 42.0, 24.0], [0.99, 0.01, 0.0]),
                    FakeFace([50.0, 6.0, 64.0, 30.0], [0.0, 1.0, 0.0]),
                ]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")
            self.enable_face_recognition_config()

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            main_conn = db.connect(target)
            try:
                file_id = int(main_conn.execute("SELECT id FROM files").fetchone()["id"])
            finally:
                main_conn.close()
            with (
                patch("bildebank.face.load_face_app", return_value=FakeApp()),
                patch("bildebank.face.read_image", return_value=object()),
            ):
                self.assertEqual(run_cli(["--target", str(target), "face-scan", "--limit", "1"]), 0)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "face-person-add-face", "Krai", "1"]
            )

            self.assertEqual(code, 1)
            self.assertIn("Fant ikke person: Krai", stderr)

            self.assertEqual(run_cli(["--target", str(target), "face-person-create", "Kari"]), 0)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "face-person-add-face", "Kari", "1"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Person: Kari", stdout)
            self.assertIn("Ansikt-id: 1", stdout)
            self.assertIn("Ansiktet er koblet til personen.", stdout)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "face-person-remove-face", "Kari", "1"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Ansiktet er fjernet fra personen.", stdout)

            self.assertEqual(
                run_cli(["--target", str(target), "face-person-add-face", "Kari", "1"]),
                0,
            )

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "face-suggest", "--threshold", "0.9"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("personer=1", stdout)
            self.assertIn("ukjente_ansikter=2", stdout)
            self.assertIn("forslag=1", stdout)
            self.assertNotIn("Forslag:", stdout)
            self.assertNotIn("Kari\tface-id=2", stdout)
            self.assertNotIn("Skrev person-index", stdout)
            self.assertFalse((target / "personer.html").exists())
            self.assertFalse((target / "person-Kari.html").exists())

            code, stdout, stderr = capture_cli(["--target", str(target), "face-report"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Personstatus:", stdout)
            self.assertIn("Personer registrert: 1", stdout)
            self.assertIn("Bekreftede ansiktskoblinger: 1", stdout)
            self.assertIn("Forslag: 1", stdout)
            self.assertIn("Bilder med minst én bekreftet person: 1", stdout)
            self.assertIn("Bilder med ansikter, men ingen bekreftet person: 0", stdout)
            self.assertIn("Bilder med både bekreftede og ukjente ansikter: 1", stdout)

            code, stdout, stderr = capture_cli(["--target", str(target), "face-person-list"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Navn  Bilder  Ansikter  Forslag  Oppdatert", stdout)
            self.assertRegex(stdout, r"Kari\s+1\s+1\s+1\s+\d{4}-\d{2}-\d{2}")

            code, stdout, stderr = capture_cli(["--target", str(target), "face-person-rename", "Kari", "Kari Nordmann"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Endret personnavn: Kari -> Kari Nordmann", stdout)

            code, stdout, stderr = capture_cli(["--target", str(target), "face-person-list"])

            self.assertEqual(code, 0, stderr)
            self.assertRegex(stdout, r"Kari Nordmann\s+1\s+1\s+1\s+\d{4}-\d{2}-\d{2}")
            self.assertNotIn("Kari  ", stdout)

            code, stdout, stderr = capture_cli(["--target", str(target), "face-person-rename", "Kari", "Kari Nordmann"])

            self.assertEqual(code, 1)
            self.assertIn("Fant ikke person: Kari", stderr)

            self.assertEqual(run_cli(["--target", str(target), "face-person-create", "Ola"]), 0)
            code, stdout, stderr = capture_cli(["--target", str(target), "face-person-rename", "Kari Nordmann", "Ola"])

            self.assertEqual(code, 1)
            self.assertIn("Person finnes allerede: Ola", stderr)

            with patch("builtins.input", return_value="slett Ola"):
                self.assertEqual(run_cli(["--target", str(target), "face-person-delete", "Ola"]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "face-person-rename", "Kari Nordmann", "Kari"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Endret personnavn: Kari Nordmann -> Kari", stdout)

            with patch("builtins.input", return_value="slett Kari"):
                code, stdout, stderr = capture_cli(["--target", str(target), "face-person-delete", "Kari"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Slettet person: Kari", stdout)
            self.assertIn("Fjernet bekreftede ansiktskoblinger: 1", stdout)
            self.assertIn("Fjernet ansiktsforslag: 1", stdout)
            self.assertIn("Ingen bilder eller scannede ansikter er slettet.", stdout)

            code, stdout, stderr = capture_cli(["--target", str(target), "face-person-list"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Ingen personer registrert.", stdout)

            self.assertEqual(run_cli(["--target", str(target), "face-person-create", "Kari"]), 0)
            self.assertEqual(run_cli(["--target", str(target), "face-person-add-face", "Kari", "1"]), 0)
            self.assertEqual(run_cli(["--target", str(target), "face-suggest", "--threshold", "0.9"]), 0)
            self.assertFalse((target / "personer.html").exists())
            main_conn = db.connect(target)
            try:
                main_conn.execute(
                    "UPDATE files SET view_rotation_degrees = 270 WHERE id = ?",
                    (file_id,),
                )
                main_conn.commit()
            finally:
                main_conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "make-person-browser", "Kari"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev HTML-browser for person", stdout)
            html = (target / "person-Kari.html").read_text(encoding="utf-8")
            self.assertIn("<title>Kari</title>", html)
            self.assertIn('id="title" class="title"', html)
            self.assertIn('<nav class="breadcrumb" aria-label="Plassering">', html)
            self.assertIn('parts.push({ label: "År", action: showYears });', html)
            self.assertIn('title="Forrige måned">◀ Mån</button>', html)
            self.assertIn('title="Neste måned">ed ▶</button>', html)
            self.assertIn("const embeddedItems", html)
            self.assertIn("IMG_20240102.jpg", html)
            self.assertIn('"kind": "image"', html)
            self.assertIn('"viewRotation": 270', html)
            self.assertNotIn('"faceId": 1', html)
            self.assertNotIn('"status": "bekreftet"', html)
            self.assertNotIn('"faceId": 2', html)
            self.assertNotIn('"status": "forslag"', html)
            self.assertNotIn('"box suggested"', html)
            self.assertNotIn("const imageRect = img.getBoundingClientRect();", html)

            code, stdout, stderr = capture_cli(["--target", str(target), "make-people-browser"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev person-index", stdout)
            self.assertIn("Skrev personsider: 1", stdout)
            index_html = (target / "personer.html").read_text(encoding="utf-8")
            self.assertIn("<h1>Personer (1)</h1>", index_html)
            self.assertIn("person-Kari.html", index_html)
            self.assertIn('href="person-Kari.html"', index_html)
            self.assertNotIn(str(target), index_html)
            self.assertIn("Kari", index_html)
            self.assertIn("1 bilder", index_html)
            self.assertIn("1 bekreftet, 1 forslag", index_html)
            self.assertIn('data-view-rotation="270"', index_html)

            config = load_config(self.program_root).face_recognition
            conn = connect_raw_face_db(target, config)
            try:
                suggestion = conn.execute(
                    """
                    SELECT persons.name, face_suggestions.face_id
                    FROM face_suggestions
                    JOIN persons ON persons.id = face_suggestions.person_id
                    """
                ).fetchone()
                self.assertEqual(suggestion, ("Kari", 2))
            finally:
                conn.close()

    def test_face_reset_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            config = load_config(self.program_root).face_recognition
            face_db = face_db_path(target, config)
            face_db.parent.mkdir(parents=True, exist_ok=True)
            face_db.write_bytes(b"face-data")

            with patch("builtins.input", return_value="nei"):
                code, stdout, stderr = capture_cli(["--target", str(target), "face-reset", "--all"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Avbrutt", stdout)
            self.assertTrue(face_db.exists())

            with patch("builtins.input", return_value="ja, slett ansiktsdata"):
                code, stdout, stderr = capture_cli(["--target", str(target), "face-reset", "--all"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Slettet face-database", stdout)
            self.assertFalse(face_db.exists())

    def test_face_reset_all_refuses_to_delete_database_while_target_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            config = load_config(self.program_root).face_recognition
            face_db = face_db_path(target, config)
            face_db.parent.mkdir(parents=True, exist_ok=True)
            face_db.write_bytes(b"face-data")
            (target / LOCK_FILENAME).write_text("command=face-scan\n", encoding="utf-8")

            with patch("builtins.input", return_value="ja, slett ansiktsdata"):
                code, _stdout, stderr = capture_cli(["--target", str(target), "face-reset", "--all"])

            self.assertEqual(code, 1)
            self.assertIn("Bildesamlingen er låst", stderr)
            self.assertTrue(face_db.exists())

    def test_face_reset_can_keep_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            self.enable_face_recognition_config()
            self.assertEqual(run_cli(["create", str(target)]), 0)
            config = load_config(self.program_root).face_recognition
            conn = connect_raw_face_db(target, config)
            try:
                conn.executescript(
                    """
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE scanned_files (
                        file_id INTEGER PRIMARY KEY,
                        target_path TEXT NOT NULL,
                        target_path_key TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        status TEXT NOT NULL,
                        error_message TEXT,
                        face_count INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE faces (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        file_id INTEGER NOT NULL,
                        target_path_key TEXT NOT NULL,
                        bbox_x REAL NOT NULL,
                        bbox_y REAL NOT NULL,
                        bbox_width REAL NOT NULL,
                        bbox_height REAL NOT NULL,
                        detection_score REAL NOT NULL,
                        embedding_model TEXT NOT NULL,
                        embedding BLOB NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE face_group_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        threshold REAL NOT NULL,
                        method TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE face_groups (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id INTEGER NOT NULL,
                        group_index INTEGER NOT NULL,
                        member_count INTEGER NOT NULL
                    );
                    CREATE TABLE face_group_members (
                        group_id INTEGER NOT NULL,
                        face_id INTEGER NOT NULL,
                        similarity REAL NOT NULL,
                        PRIMARY KEY(group_id, face_id)
                    );
                    CREATE TABLE persons (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE person_faces (
                        person_id INTEGER NOT NULL,
                        face_id INTEGER NOT NULL,
                        confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(person_id, face_id)
                    );
                    CREATE TABLE face_suggestions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        person_id INTEGER NOT NULL,
                        face_id INTEGER NOT NULL,
                        similarity REAL NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(person_id, face_id)
                    );
                    INSERT INTO meta(key, value) VALUES('schema_version', '2');
                    INSERT INTO scanned_files(file_id, target_path, target_path_key, sha256, status, face_count)
                    VALUES(1, 'image.jpg', 'image.jpg', 'hash', 'ok', 1);
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    ) VALUES(1, 1, 'image.jpg', 1, 2, 10, 20, 0.9, 'test', x'00000000');
                    INSERT INTO face_group_runs(id, threshold, method) VALUES(1, 0.6, 'test');
                    INSERT INTO face_groups(id, run_id, group_index, member_count) VALUES(1, 1, 1, 1);
                    INSERT INTO face_group_members(group_id, face_id, similarity) VALUES(1, 1, 1.0);
                    INSERT INTO persons(id, name) VALUES(1, 'Kari');
                    INSERT INTO person_faces(person_id, face_id) VALUES(1, 1);
                    INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 1, 0.95);
                    """
                )
                conn.commit()
            finally:
                conn.close()

            with patch("builtins.input", return_value="ja, slett personer"):
                code, stdout, stderr = capture_cli(["--target", str(target), "face-reset", "--keep-scan"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Face-scan-resultater er beholdt", stdout)
            conn = connect_raw_face_db(target, config)
            try:
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "5")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM person_files").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM scanned_files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM person_faces").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM person_files").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM face_suggestions").fetchone()[0], 0)
                legacy_tables = {
                    row[0]
                    for row in conn.execute(
                        """
                        SELECT name
                        FROM sqlite_master
                        WHERE type = 'table' AND name LIKE 'face_group_%'
                        """
                    )
                }
                self.assertEqual(legacy_tables, set())
            finally:
                conn.close()

            conn = connect_raw_face_db(target, config)
            try:
                conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)")
                conn.execute("INSERT INTO person_files(person_id, file_id) VALUES(1, 1)")
                conn.execute("INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 1, 0.95)")
                conn.commit()
            finally:
                conn.close()

            with patch("builtins.input", return_value="ja, slett personer"):
                code, stdout, stderr = capture_cli(["--target", str(target), "face-reset"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Face-scan-resultater er beholdt", stdout)
            conn = connect_raw_face_db(target, config)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM scanned_files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM person_files").fetchone()[0], 0)
            finally:
                conn.close()

    def test_face_schema_v2_migration_drops_legacy_group_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            conn = connect_raw_face_db(target)
            try:
                conn.executescript(
                    """
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE scanned_files (
                        file_id INTEGER PRIMARY KEY,
                        target_path TEXT NOT NULL,
                        target_path_key TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        status TEXT NOT NULL,
                        error_message TEXT,
                        face_count INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE face_group_runs (id INTEGER PRIMARY KEY AUTOINCREMENT);
                    CREATE TABLE face_groups (id INTEGER PRIMARY KEY AUTOINCREMENT);
                    CREATE TABLE face_group_members (group_id INTEGER NOT NULL, face_id INTEGER NOT NULL);
                    INSERT INTO meta(key, value) VALUES('schema_version', '2');
                    INSERT INTO scanned_files(file_id, target_path, target_path_key, sha256, status, face_count)
                    VALUES(1, 'image.jpg', 'image.jpg', 'hash', 'ok', 0);
                    """
                )
                conn.commit()
            finally:
                conn.close()

            conn = connect_face_db(target)
            try:
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "5")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM scanned_files").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM person_files").fetchone()[0], 0)
                legacy_tables = {
                    row[0]
                    for row in conn.execute(
                        """
                        SELECT name
                        FROM sqlite_master
                        WHERE type = 'table' AND name LIKE 'face_group_%'
                        """
                    )
                }
                self.assertEqual(legacy_tables, set())
            finally:
                conn.close()

    def test_face_schema_v3_migration_adds_person_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            conn = connect_raw_face_db(target)
            try:
                conn.executescript(
                    """
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE scanned_files (
                        file_id INTEGER PRIMARY KEY,
                        target_path TEXT NOT NULL,
                        target_path_key TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        status TEXT NOT NULL,
                        error_message TEXT,
                        face_count INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE faces (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        file_id INTEGER NOT NULL,
                        target_path_key TEXT NOT NULL,
                        bbox_x REAL NOT NULL,
                        bbox_y REAL NOT NULL,
                        bbox_width REAL NOT NULL,
                        bbox_height REAL NOT NULL,
                        detection_score REAL NOT NULL,
                        embedding_model TEXT NOT NULL,
                        embedding BLOB NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE persons (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE person_faces (
                        person_id INTEGER NOT NULL,
                        face_id INTEGER NOT NULL,
                        confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(person_id, face_id)
                    );
                    CREATE TABLE face_suggestions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        person_id INTEGER NOT NULL,
                        face_id INTEGER NOT NULL,
                        similarity REAL NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(person_id, face_id)
                    );
                    INSERT INTO meta(key, value) VALUES('schema_version', '3');
                    INSERT INTO scanned_files(file_id, target_path, target_path_key, sha256, status, face_count)
                    VALUES(1, 'image.jpg', 'image.jpg', 'hash', 'ok', 1);
                    INSERT INTO persons(id, name) VALUES(1, 'Kari');
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    ) VALUES(1, 1, 'image.jpg', 1, 2, 10, 20, 0.9, 'test', x'00000000');
                    INSERT INTO person_faces(person_id, face_id) VALUES(1, 1);
                    INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 1, 0.95);
                    """
                )
                conn.commit()
            finally:
                conn.close()

            conn = connect_face_db(target)
            try:
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "5")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM person_faces").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM face_suggestions").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM person_files").fetchone()[0], 0)
            finally:
                conn.close()

    def test_face_schema_v4_migration_adds_reference_face_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            conn = connect_raw_face_db(target)
            try:
                conn.executescript(
                    """
                    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE persons (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE face_suggestions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        person_id INTEGER NOT NULL,
                        face_id INTEGER NOT NULL,
                        similarity REAL NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(person_id, face_id)
                    );
                    INSERT INTO meta(key, value) VALUES('schema_version', '4');
                    INSERT INTO persons(id, name) VALUES(1, 'Kari');
                    INSERT INTO face_suggestions(person_id, face_id, similarity)
                    VALUES(1, 10, 0.95);
                    """
                )
                conn.commit()
            finally:
                conn.close()

            conn = connect_face_db(target)
            try:
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "5")
                columns = {row[1] for row in conn.execute("PRAGMA table_info(face_suggestions)")}
                self.assertIn("reference_face_id", columns)
                suggestion = conn.execute(
                    "SELECT person_id, face_id, reference_face_id, similarity FROM face_suggestions"
                ).fetchone()
                self.assertEqual(tuple(suggestion), (1, 10, None, 0.95))
                indexes = {row[1] for row in conn.execute("PRAGMA index_list(face_suggestions)")}
                self.assertIn("idx_face_suggestions_reference_face_id", indexes)
            finally:
                conn.close()

    def test_face_schema_v4_migration_rolls_back_late_failure_and_keeps_backup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            path = create_face_v4_database(target)
            wal_conn = sqlite3.connect(path)
            try:
                self.assertEqual(
                    wal_conn.execute("PRAGMA journal_mode = WAL").fetchone()[0],
                    "wal",
                )
                wal_conn.execute(
                    "UPDATE persons SET name = 'Kari fra WAL' WHERE id = 1"
                )
                wal_conn.commit()
                self.assertTrue(
                    path.with_name(f"{path.name}-wal").exists()
                )
                before_dump = face_database_dump(path)

                with patch(
                    "bildebank.face.validate_current_face_schema",
                    side_effect=RuntimeError("injisert sen face-feil"),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "injisert sen face-feil",
                    ):
                        connect_face_db(target)
            finally:
                wal_conn.close()

            self.assertEqual(face_database_dump(path), before_dump)
            backups = list(
                path.parent.glob(
                    f"{path.name}.backup-before-schema-5-*"
                )
            )
            self.assertEqual(len(backups), 1)
            self.assertEqual(face_database_dump(backups[0]), before_dump)

            conn = connect_face_db(target)
            try:
                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM meta WHERE key = 'schema_version'"
                    ).fetchone()[0],
                    "5",
                )
                self.assertEqual(
                    tuple(
                        conn.execute(
                            """
                            SELECT person_id, face_id, reference_face_id, similarity
                            FROM face_suggestions
                            """
                        ).fetchone()
                    ),
                    (1, 10, None, 0.95),
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM person_faces").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM person_files").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute("SELECT name FROM persons WHERE id = 1").fetchone()[0],
                    "Kari fra WAL",
                )
                self.assertEqual(
                    conn.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
                self.assertEqual(
                    conn.execute("PRAGMA foreign_key_check").fetchall(),
                    [],
                )
            finally:
                conn.close()
            self.assertEqual(
                len(
                    list(
                        path.parent.glob(
                            f"{path.name}.backup-before-schema-5-*"
                        )
                    )
                ),
                2,
            )

    def test_face_schema_v2_keyboard_interrupt_restores_legacy_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            path = create_face_v4_database(target)
            conn = sqlite3.connect(path)
            try:
                conn.execute("DROP INDEX idx_person_files_file_id")
                conn.execute("DROP TABLE person_files")
                conn.execute(
                    "CREATE TABLE face_group_runs (id INTEGER PRIMARY KEY)"
                )
                conn.execute(
                    "CREATE TABLE face_groups (id INTEGER PRIMARY KEY)"
                )
                conn.execute(
                    """
                    CREATE TABLE face_group_members (
                        group_id INTEGER NOT NULL,
                        face_id INTEGER NOT NULL
                    )
                    """
                )
                conn.execute("INSERT INTO face_group_runs(id) VALUES(1)")
                conn.execute("INSERT INTO face_groups(id) VALUES(1)")
                conn.execute(
                    "INSERT INTO face_group_members(group_id, face_id) VALUES(1, 1)"
                )
                conn.execute(
                    "UPDATE meta SET value = '2' WHERE key = 'schema_version'"
                )
                conn.commit()
            finally:
                conn.close()
            before_dump = face_database_dump(path)

            with patch(
                "bildebank.face.validate_current_face_schema",
                side_effect=KeyboardInterrupt,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    connect_face_db(target)

            self.assertEqual(face_database_dump(path), before_dump)
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM face_group_members").fetchone()[0],
                    1,
                )
            finally:
                conn.close()

            conn = connect_face_db(target)
            try:
                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM meta WHERE key = 'schema_version'"
                    ).fetchone()[0],
                    "5",
                )
                self.assertFalse(
                    conn.execute(
                        """
                        SELECT 1 FROM sqlite_master
                        WHERE type = 'table' AND name = 'face_group_members'
                        """
                    ).fetchone()
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM person_faces").fetchone()[0],
                    1,
                )
            finally:
                conn.close()

    def test_face_schema_v4_migration_recovers_old_partial_v5_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            path = create_face_v4_database(target, partial_v5=True)

            conn = connect_face_db(target)
            try:
                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM meta WHERE key = 'schema_version'"
                    ).fetchone()[0],
                    "5",
                )
                self.assertIn(
                    "reference_face_id",
                    {
                        row[1]
                        for row in conn.execute(
                            "PRAGMA table_info(face_suggestions)"
                        )
                    },
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM face_suggestions"
                    ).fetchone()[0],
                    1,
                )
            finally:
                conn.close()

            self.assertEqual(
                len(
                    list(
                        path.parent.glob(
                            f"{path.name}.backup-before-schema-5-*"
                        )
                    )
                ),
                1,
            )

    def test_face_schema_backup_failure_prevents_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            path = create_face_v4_database(target)
            before_dump = face_database_dump(path)

            with patch(
                "bildebank.face.backup_face_database",
                side_effect=OSError("injisert backupfeil"),
            ):
                with self.assertRaisesRegex(OSError, "injisert backupfeil"):
                    connect_face_db(target)

            self.assertEqual(face_database_dump(path), before_dump)

    def test_concurrent_face_schema_open_migrates_v4_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            path = create_face_v4_database(target)

            def open_and_close() -> None:
                conn = connect_face_db(target)
                conn.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(open_and_close) for _ in range(2)]
                for future in futures:
                    future.result()

            conn = connect_face_db(target)
            try:
                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM meta WHERE key = 'schema_version'"
                    ).fetchone()[0],
                    "5",
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM person_faces").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM person_files").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    conn.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
            finally:
                conn.close()

            self.assertGreaterEqual(
                len(
                    list(
                        path.parent.glob(
                            f"{path.name}.backup-before-schema-5-*"
                        )
                    )
                ),
                1,
            )

    def test_face_schema_does_not_migrate_database_for_other_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            path = create_face_v4_database(target)
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('model_name', 'wrong-model')"
                )
                conn.commit()
            finally:
                conn.close()
            before_dump = face_database_dump(path)

            with self.assertRaisesRegex(
                ValueError,
                "tilhører en annen modell",
            ):
                connect_face_db(target)

            self.assertEqual(face_database_dump(path), before_dump)
            self.assertEqual(
                list(
                    path.parent.glob(
                        f"{path.name}.backup-before-schema-5-*"
                    )
                ),
                [],
            )

    def test_face_schema_current_version_rejects_legacy_group_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            conn = connect_raw_face_db(target)
            try:
                apply_face_schema(conn)
                conn.execute("CREATE TABLE face_group_runs (id INTEGER PRIMARY KEY)")
                conn.execute("INSERT INTO face_group_runs(id) VALUES(1)")
                conn.commit()

                with self.assertRaisesRegex(ValueError, "legacy-gruppetabeller"):
                    apply_face_schema(conn)

                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "5")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM face_group_runs").fetchone()[0], 1)
            finally:
                conn.close()

    def test_face_schema_current_version_rejects_missing_tables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            path = face_db_path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', '5')"
                )
                conn.commit()
            finally:
                conn.close()
            before = face_database_dump(path)

            with self.assertRaisesRegex(
                ValueError,
                "mangler forventede tabeller",
            ):
                opened = connect_face_db(target)
                opened.close()

            self.assertEqual(face_database_dump(path), before)

    def test_face_schema_current_version_rejects_missing_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            path = face_db_path(target)
            conn = connect_face_db(target)
            conn.close()
            conn = sqlite3.connect(path)
            try:
                conn.execute("ALTER TABLE faces DROP COLUMN embedding")
                conn.commit()
            finally:
                conn.close()

            with self.assertRaisesRegex(
                ValueError,
                "faces mangler forventede kolonner: embedding",
            ):
                opened = connect_face_db(target)
                opened.close()

    def test_face_schema_current_version_rejects_foreign_key_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            path = face_db_path(target)
            conn = connect_face_db(target)
            conn.close()
            conn = sqlite3.connect(path)
            try:
                conn.execute(
                    "INSERT INTO person_faces(person_id, face_id) VALUES(999, 1)"
                )
                conn.commit()
            finally:
                conn.close()

            with self.assertRaisesRegex(ValueError, "foreign_key_check feilet"):
                opened = connect_face_db(target)
                opened.close()

    def test_connect_face_db_enables_foreign_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)

            conn = connect_face_db(target)
            try:
                self.assertEqual(
                    conn.execute("PRAGMA foreign_keys").fetchone()[0],
                    1,
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "INSERT INTO person_faces(person_id, face_id) VALUES(999, 1)"
                    )
            finally:
                conn.close()

    def test_face_schema_migration_health_failure_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            path = create_face_v4_database(target)
            before = face_database_dump(path)

            with patch(
                "bildebank.face.db.validate_database_health",
                side_effect=RuntimeError("injisert face-integritetsfeil"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injisert face-integritetsfeil",
                ):
                    opened = connect_face_db(target)
                    opened.close()

            self.assertEqual(face_database_dump(path), before)
