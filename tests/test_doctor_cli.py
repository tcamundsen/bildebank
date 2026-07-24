from __future__ import annotations

import sqlite3
import stat
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

    def test_doctor_and_deep_doctor_leave_all_files_unchanged(self) -> None:
        config_path = self.program_root / "bildebank-config.toml"
        legacy_config = (
            "[face_recognition]\n"
            'model_name = "buffalo_l"\n'
            "\n"
            "[openclip]\n"
            "enabled = false\n"
        )

        for deep in (False, True):
            with self.subTest(deep=deep), tempfile.TemporaryDirectory() as tmp:
                config_path.write_text(legacy_config, encoding="utf-8")
                target = Path(tmp) / "target"
                init_database(target)
                stored_path = target / "2024" / "01" / "pending.jpg"
                stored_path.parent.mkdir(parents=True)
                stored_path.write_bytes(b"pending")
                file_id = register_target_file(target, stored_path.relative_to(target))

                conn = db.connect(target)
                try:
                    db.create_pending_file_move(
                        conn,
                        file_id=file_id,
                        target_root=target,
                        from_path=stored_path,
                        to_path=target / "deleted" / "2024" / "01" / stored_path.name,
                        sha256=sha256_file(stored_path),
                        operation="remove",
                    )
                    conn.commit()
                finally:
                    conn.close()

                legacy_face_path = target / ".bilder-faces.sqlite3"
                conn = sqlite3.connect(legacy_face_path)
                try:
                    conn.executescript(
                        """
                        CREATE TABLE scanned_files(id INTEGER);
                        CREATE TABLE faces(id INTEGER);
                        """
                    )
                    conn.commit()
                finally:
                    conn.close()

                openclip_conn = connect_openclip_db(target)
                openclip_conn.close()

                program_before = tree_image(self.program_root)
                collection_before = tree_image(target)
                args = ["--target", str(target), "doctor"]
                if deep:
                    args.append("--deep")

                with (
                    patch(
                        "bildebank.cli_doctor.resolve_exiftool_path",
                        side_effect=FileNotFoundError("mangler"),
                    ),
                    patch(
                        "bildebank.cli_doctor.resolve_ffmpeg_tools",
                        side_effect=FileNotFoundError("mangler"),
                    ),
                    patch("bildebank.cli_doctor.python_module_available", return_value=False),
                ):
                    code, _stdout, stderr = capture_cli(args)

                self.assertEqual(code, 0, stderr)
                self.assertEqual(tree_image(self.program_root), program_before)
                self.assertEqual(tree_image(target), collection_before)

    def test_doctor_shows_exiftool_status(self) -> None:
        exiftool = self.program_root / "bildebank-tools" / "exiftool" / "exiftool.exe"
        with (
            patch("bildebank.cli_doctor.resolve_exiftool_path", return_value=exiftool),
            patch("bildebank.cli_doctor.validate_exiftool_install", return_value="13.58"),
        ):
            code, stdout, stderr = capture_cli(["doctor"])

        self.assertEqual(code, 0, stderr)
        self.assertIn(f"  OK: ExifTool funnet: {exiftool} (13.58)", stdout)

    def test_doctor_reports_healthy_main_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)

            with (
                patch(
                    "bildebank.cli_doctor.resolve_exiftool_path",
                    side_effect=FileNotFoundError("mangler"),
                ),
                patch("bildebank.cli_doctor.python_module_available", return_value=False),
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "doctor"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("OK: SQLite integrity_check: ok", stdout)
        self.assertIn("OK: SQLite foreign_key_check: ingen feil", stdout)

    def test_doctor_reports_main_database_foreign_key_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                source_id = conn.execute(
                    """
                    INSERT INTO sources(path, path_key, name, status)
                    VALUES('C:\\source', 'c:/source', 'source', 'imported')
                    RETURNING id
                    """
                ).fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO file_sources(
                        file_id, source_id, source_path, source_path_key,
                        sha256, size_bytes
                    )
                    VALUES(999, ?, 'C:\\source\\missing.jpg',
                           'c:/source/missing.jpg', 'sha', 1)
                    """,
                    (source_id,),
                )
                conn.commit()
            finally:
                conn.close()

            with (
                patch(
                    "bildebank.cli_doctor.resolve_exiftool_path",
                    side_effect=FileNotFoundError("mangler"),
                ),
                patch("bildebank.cli_doctor.python_module_available", return_value=False),
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "doctor"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("OK: SQLite integrity_check: ok", stdout)
        self.assertIn(
            "FEIL: SQLite foreign_key_check fant 1 ugyldig referanse.",
            stdout,
        )
        self.assertIn("INFO: table=file_sources", stdout)
        self.assertIn("parent=files", stdout)

    def test_doctor_skips_other_checks_when_integrity_check_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)

            with (
                patch(
                    "bildebank.cli_doctor.resolve_exiftool_path",
                    side_effect=FileNotFoundError("mangler"),
                ),
                patch("bildebank.cli_doctor.python_module_available", return_value=False),
                patch(
                    "bildebank.cli_doctor.db.database_integrity_errors",
                    return_value=["database disk image is malformed"],
                ),
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "doctor"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("FEIL: SQLite integrity_check fant 1 feil.", stdout)
        self.assertIn("INFO: database disk image is malformed", stdout)
        self.assertIn("øvrige database- og filkontroller er hoppet over", stdout)
        self.assertNotIn("ingen uavklarte filflyttinger", stdout)

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

    def test_doctor_reports_active_and_deleted_files_without_source_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            active_path = target / "2024" / "01" / "active.jpg"
            deleted_path = target / "deleted" / "2024" / "01" / "deleted.jpg"
            active_path.parent.mkdir(parents=True)
            deleted_path.parent.mkdir(parents=True)
            active_path.write_bytes(b"active")
            deleted_path.write_bytes(b"deleted")
            conn = db.connect(target)
            try:
                for path, content, deleted in (
                    (active_path, b"active", False),
                    (deleted_path, b"deleted", True),
                ):
                    relative = path.relative_to(target)
                    conn.execute(
                        """
                        INSERT INTO files(
                            target_path, target_path_key, original_filename,
                            stored_filename, sha256, size_bytes, date_source,
                            deleted_at
                        )
                        VALUES(?, ?, ?, ?, ?, ?, 'filename',
                               CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END)
                        """,
                        (
                            relative.as_posix(),
                            db.relative_path_key(relative),
                            path.name,
                            path.name,
                            sha256_file(path),
                            len(content),
                            deleted,
                        ),
                    )
                conn.commit()
            finally:
                conn.close()

            with (
                patch(
                    "bildebank.cli_doctor.resolve_exiftool_path",
                    side_effect=FileNotFoundError("mangler"),
                ),
                patch("bildebank.cli_doctor.python_module_available", return_value=False),
            ):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "doctor"]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn(
                "FEIL: 2 files-rad(er) mangler file_sources-proveniens "
                "(aktive=1, slettede=1).",
                stdout,
            )
            self.assertIn("INFO: file #1 (aktiv): 2024/01/active.jpg", stdout)
            self.assertIn(
                "INFO: file #2 (slettet): deleted/2024/01/deleted.jpg",
                stdout,
            )

    def test_doctor_reports_file_source_hash_and_size_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            init_database(target)
            source.mkdir()
            active_path = target / "2024" / "01" / "active.jpg"
            deleted_path = target / "deleted" / "2024" / "01" / "deleted.jpg"
            active_path.parent.mkdir(parents=True)
            deleted_path.parent.mkdir(parents=True)
            active_path.write_bytes(b"active")
            deleted_path.write_bytes(b"deleted")

            conn = db.connect(target)
            try:
                source_id = db.add_named_source(conn, source, "source")
                active_id = db.insert_imported_file(
                    conn,
                    source_id=source_id,
                    source_path=source / active_path.name,
                    target_root=target,
                    target_path=active_path,
                    original_filename=active_path.name,
                    stored_filename=active_path.name,
                    sha256=sha256_file(active_path),
                    size_bytes=active_path.stat().st_size,
                    taken_date="2024-01-02",
                    date_source="filename",
                    name_conflict=False,
                )
                deleted_id = db.insert_imported_file(
                    conn,
                    source_id=source_id,
                    source_path=source / deleted_path.name,
                    target_root=target,
                    target_path=deleted_path,
                    original_filename=deleted_path.name,
                    stored_filename=deleted_path.name,
                    sha256=sha256_file(deleted_path),
                    size_bytes=deleted_path.stat().st_size,
                    taken_date="2024-01-03",
                    date_source="filename",
                    name_conflict=False,
                )
                conn.execute(
                    """
                    UPDATE files
                    SET deleted_at = CURRENT_TIMESTAMP,
                        deleted_original_target_path = '2024/01/deleted.jpg'
                    WHERE id = ?
                    """,
                    (deleted_id,),
                )
                conn.execute(
                    "UPDATE file_sources SET sha256 = 'wrong-hash' WHERE file_id = ?",
                    (active_id,),
                )
                conn.execute(
                    "UPDATE file_sources SET size_bytes = 999 WHERE file_id = ?",
                    (deleted_id,),
                )
                conn.commit()
            finally:
                conn.close()

            with (
                patch(
                    "bildebank.cli_doctor.resolve_exiftool_path",
                    side_effect=FileNotFoundError("mangler"),
                ),
                patch("bildebank.cli_doctor.python_module_available", return_value=False),
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "doctor"])

        self.assertEqual(code, 0, stderr)
        self.assertIn(
            "FEIL: 1 file_sources-rad(er) har SHA-256 som ikke stemmer med files.",
            stdout,
        )
        self.assertIn("file #1 (aktiv): 2024/01/active.jpg", stdout)
        self.assertIn("files.sha256=", stdout)
        self.assertIn("file_sources.sha256=wrong-hash", stdout)
        self.assertIn(
            "FEIL: 1 file_sources-rad(er) har størrelse som ikke stemmer med files.",
            stdout,
        )
        self.assertIn(
            "file #2 (slettet): deleted/2024/01/deleted.jpg",
            stdout,
        )
        self.assertIn("files.size_bytes=7, file_sources.size_bytes=999", stdout)

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


def tree_image(root: Path) -> dict[str, tuple[object, ...]]:
    image: dict[str, tuple[object, ...]] = {}
    for path in (root, *sorted(root.rglob("*"))):
        relative = "." if path == root else path.relative_to(root).as_posix()
        path_stat = path.stat(follow_symlinks=False)
        common = (
            stat.S_IMODE(path_stat.st_mode),
            path_stat.st_size,
            path_stat.st_mtime_ns,
        )
        if path.is_dir():
            image[relative] = ("directory", *common)
        else:
            image[relative] = ("file", *common, path.read_bytes())
    return image
