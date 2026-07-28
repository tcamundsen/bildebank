from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.cli import main
from bildebank.config import FaceRecognitionConfig
from bildebank.face import connect_face_db, face_db_path
from bildebank.media import sha256_file
from bildebank.target_lock import LOCK_FILENAME
from tests.cli_helpers import capture_cli
from tests.db_test_helpers import insert_test_file


def face_database_dump(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


def insert_face_path_mismatch(
    target: Path,
    *,
    sidecar_sha256: str | None = None,
) -> tuple[int, Path, str, tuple[FaceRecognitionConfig, ...]]:
    relative_path = Path("2024/01/current.png")
    media_path = target / relative_path
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_bytes(b"current image content")
    expected_sha256 = sha256_file(media_path)
    file_id = insert_test_file(
        target,
        relative_path.as_posix(),
        sha256=expected_sha256,
    )
    configs = (
        FaceRecognitionConfig(model_name="buffalo_l"),
        FaceRecognitionConfig(model_name="antelopev2"),
    )
    for config_index, config in enumerate(configs):
        conn = connect_face_db(target, config)
        try:
            face_count = config_index + 1
            conn.execute(
                """
                INSERT INTO scanned_files(
                    file_id, target_path, target_path_key, sha256,
                    status, face_count
                ) VALUES(
                    ?, '2023/12/old.png', '2023/12/old.png', ?,
                    'ok', ?
                )
                """,
                (
                    file_id,
                    sidecar_sha256 or expected_sha256,
                    face_count,
                ),
            )
            for face_index in range(face_count):
                conn.execute(
                    """
                    INSERT INTO faces(
                        file_id, target_path_key, bbox_x, bbox_y,
                        bbox_width, bbox_height, detection_score,
                        embedding_model, embedding
                    ) VALUES(
                        ?, '2023/12/old.png', 1, 2, 3, 4, 0.9, ?, ?
                    )
                    """,
                    (
                        file_id,
                        config.model_name,
                        f"embedding-{config_index}-{face_index}".encode(),
                    ),
                )
            person_id = int(
                conn.execute(
                    "INSERT INTO persons(name) VALUES(?) RETURNING id",
                    (f"Person {config.model_name}",),
                ).fetchone()[0]
            )
            face_id = int(
                conn.execute(
                    "SELECT id FROM faces WHERE file_id = ? ORDER BY id LIMIT 1",
                    (file_id,),
                ).fetchone()[0]
            )
            conn.execute(
                "INSERT INTO person_faces(person_id, face_id) VALUES(?, ?)",
                (person_id, face_id),
            )
            conn.execute(
                "INSERT INTO person_files(person_id, file_id) VALUES(?, ?)",
                (person_id, file_id),
            )
            conn.commit()
        finally:
            conn.close()
    return file_id, media_path, expected_sha256, configs


class FacePathRepairCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.program_root_tempdir = tempfile.TemporaryDirectory()
        self.program_root = Path(self.program_root_tempdir.name)
        self.program_root_patcher = patch(
            "bildebank.cli.program_repo_root",
            return_value=self.program_root,
        )
        self.program_root_patcher.start()

    def tearDown(self) -> None:
        self.program_root_patcher.stop()
        self.program_root_tempdir.cleanup()

    def test_help_documents_dry_run_and_apply(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with (
            redirect_stdout(stdout_buffer),
            redirect_stderr(stderr_buffer),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["repair-face-paths", "-h"])

        help_text = stdout_buffer.getvalue()
        normalized_help = " ".join(help_text.split())

        self.assertEqual(raised.exception.code, 0)
        self.assertIn(
            "usage: bildebank repair-face-paths [valg]",
            help_text,
        )
        self.assertIn("--apply", help_text)
        self.assertIn("personkoblinger endres ikke", normalized_help)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_reports_missing_database_without_creating_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            db.init_database(target)

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "repair-face-paths"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertEqual(stdout, "Ingen InsightFace-database å reparere.\n")
            self.assertFalse((target / ".bildebank-faces").exists())

    def test_dry_run_then_apply_updates_only_paths_in_all_model_databases(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            db.init_database(target)
            file_id, media_path, _sha256, configs = insert_face_path_mismatch(
                target
            )
            paths = tuple(face_db_path(target, config) for config in configs)
            before_dumps = tuple(face_database_dump(path) for path in paths)
            main_row_before = read_main_file_row(target, file_id)
            media_before = media_path.read_bytes()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "repair-face-paths"]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("databaser=2", stdout)
            self.assertIn("reparerbare_filer=2", stdout)
            self.assertIn("scanned_files-rader=2", stdout)
            self.assertIn("faces-rader=3", stdout)
            self.assertIn("Dry-run: ingen endringer er gjort.", stdout)
            self.assertIn(
                "Kjør: bildebank repair-face-paths --apply",
                stdout,
            )
            self.assertEqual(
                tuple(face_database_dump(path) for path in paths),
                before_dumps,
            )

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "repair-face-paths",
                    "--apply",
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Oppdatert: scanned_files=2, faces=3", stdout)
            for config, path in zip(configs, paths, strict=True):
                conn = sqlite3.connect(path)
                try:
                    scanned = conn.execute(
                        """
                        SELECT target_path, target_path_key, sha256, face_count
                        FROM scanned_files WHERE file_id = ?
                        """,
                        (file_id,),
                    ).fetchone()
                    faces = conn.execute(
                        """
                        SELECT target_path_key, embedding_model, embedding
                        FROM faces WHERE file_id = ? ORDER BY id
                        """,
                        (file_id,),
                    ).fetchall()
                    persons = conn.execute(
                        "SELECT name FROM persons ORDER BY id"
                    ).fetchall()
                    person_faces = conn.execute(
                        "SELECT person_id, face_id FROM person_faces"
                    ).fetchall()
                    person_files = conn.execute(
                        "SELECT person_id, file_id FROM person_files"
                    ).fetchall()
                finally:
                    conn.close()
                self.assertEqual(
                    scanned[:2],
                    ("2024/01/current.png", "2024/01/current.png"),
                )
                self.assertTrue(
                    all(
                        row[0] == "2024/01/current.png"
                        and row[1] == config.model_name
                        and bytes(row[2]).startswith(b"embedding-")
                        for row in faces
                    )
                )
                self.assertEqual(persons, [(f"Person {config.model_name}",)])
                self.assertEqual(len(person_faces), 1)
                self.assertEqual(person_files, [(1, file_id)])
            self.assertEqual(read_main_file_row(target, file_id), main_row_before)
            self.assertEqual(media_path.read_bytes(), media_before)
            self.assertFalse((target / LOCK_FILENAME).exists())

    def test_apply_refuses_sha_mismatch_without_partial_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            db.init_database(target)
            _file_id, _media_path, _sha256, configs = (
                insert_face_path_mismatch(
                    target,
                    sidecar_sha256="different-sha256",
                )
            )
            paths = tuple(face_db_path(target, config) for config in configs)
            before_dumps = tuple(face_database_dump(path) for path in paths)

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "repair-face-paths",
                    "--apply",
                ]
            )

            self.assertEqual(code, 2, stderr)
            self.assertIn("reparerbare_filer=0", stdout)
            self.assertNotIn("andre_avvik_som_ikke_røres=0", stdout)
            self.assertIn("ble ikke reparert", stdout)
            self.assertEqual(
                tuple(face_database_dump(path) for path in paths),
                before_dumps,
            )

    def test_apply_refuses_changed_media_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            db.init_database(target)
            _file_id, media_path, _sha256, configs = (
                insert_face_path_mismatch(target)
            )
            paths = tuple(face_db_path(target, config) for config in configs)
            before_dumps = tuple(face_database_dump(path) for path in paths)
            media_path.write_bytes(b"changed after database registration")

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "repair-face-paths",
                    "--apply",
                ]
            )

            self.assertEqual(code, 1, stdout)
            self.assertEqual(stdout, "")
            self.assertIn(
                "InsightFace-stiene kan ikke repareres trygt",
                stderr,
            )
            self.assertIn("Ingen face-stier ble endret", stderr)
            self.assertEqual(
                tuple(face_database_dump(path) for path in paths),
                before_dumps,
            )

    def test_apply_refuses_pending_move_without_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            db.init_database(target)
            file_id, media_path, expected_sha256, configs = (
                insert_face_path_mismatch(target)
            )
            conn = db.connect(target)
            try:
                db.create_pending_file_move(
                    conn,
                    file_id=file_id,
                    target_root=target,
                    from_path=media_path,
                    to_path=target / "deleted" / "2024/01/current.png",
                    sha256=expected_sha256,
                    operation="remove",
                )
                conn.commit()
            finally:
                conn.close()
            paths = tuple(face_db_path(target, config) for config in configs)
            before_dumps = tuple(face_database_dump(path) for path in paths)

            with patch(
                "bildebank.cli.recover_pending_file_moves"
            ) as recover:
                code, stdout, stderr = capture_cli(
                    [
                        "--target",
                        str(target),
                        "repair-face-paths",
                        "--apply",
                    ]
                )

            self.assertEqual(code, 1, stdout)
            recover.assert_not_called()
            self.assertIn("uavklart(e) filflytting", stderr)
            self.assertEqual(
                tuple(face_database_dump(path) for path in paths),
                before_dumps,
            )

    def test_dry_run_rejects_old_schema_without_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            db.init_database(target)
            config = FaceRecognitionConfig(model_name="buffalo_l")
            conn = connect_face_db(target, config)
            try:
                conn.execute(
                    "UPDATE meta SET value = '4' WHERE key = 'schema_version'"
                )
                conn.commit()
            finally:
                conn.close()
            path = face_db_path(target, config)
            before = path.read_bytes()

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "repair-face-paths"]
            )

            self.assertEqual(code, 1, stdout)
            self.assertIn("read-only-åpning krever schema_version=5", stderr)
            self.assertEqual(path.read_bytes(), before)

    def test_doctor_recommends_repair_only_until_paths_are_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            db.init_database(target)
            file_id, _media_path, _sha256, configs = (
                insert_face_path_mismatch(target)
            )
            active_config = configs[1]
            conn = sqlite3.connect(face_db_path(target, active_config))
            try:
                conn.execute(
                    """
                    UPDATE scanned_files
                    SET target_path = '2024/01/current.png',
                        target_path_key = '2024/01/current.png'
                    WHERE file_id = ?
                    """,
                    (file_id,),
                )
                conn.execute(
                    """
                    UPDATE faces
                    SET target_path_key = '2024/01/current.png'
                    WHERE file_id = ?
                    """,
                    (file_id,),
                )
                conn.commit()
            finally:
                conn.close()

            with (
                patch(
                    "bildebank.cli_doctor.resolve_exiftool",
                    side_effect=FileNotFoundError("mangler"),
                ),
                patch(
                    "bildebank.cli_doctor.python_module_available",
                    return_value=False,
                ),
            ):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "doctor"]
                )

            self.assertEqual(code, 0, stderr)
            self.assertIn(
                "InsightFace-schema og intern konsistens: 1/2 databaser ok",
                stdout,
            )
            self.assertIn(
                "Kjør `bildebank repair-face-paths` for en dry-run",
                stdout,
            )

            code, _stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "repair-face-paths",
                    "--apply",
                ]
            )
            self.assertEqual(code, 0, stderr)

            with (
                patch(
                    "bildebank.cli_doctor.resolve_exiftool",
                    side_effect=FileNotFoundError("mangler"),
                ),
                patch(
                    "bildebank.cli_doctor.python_module_available",
                    return_value=False,
                ),
            ):
                code, stdout, stderr = capture_cli(
                    ["--target", str(target), "doctor"]
                )

            self.assertEqual(code, 0, stderr)
            self.assertNotIn("repair-face-paths", stdout)
            self.assertIn(
                "InsightFace-schema og intern konsistens: 2/2 databaser ok",
                stdout,
            )


def read_main_file_row(target: Path, file_id: int) -> tuple[object, ...]:
    conn = sqlite3.connect(db.db_path_for_target(target))
    try:
        row = conn.execute(
            """
            SELECT target_path, target_path_key, sha256, size_bytes
            FROM files WHERE id = ?
            """,
            (file_id,),
        ).fetchone()
        assert row is not None
        return tuple(row)
    finally:
        conn.close()
