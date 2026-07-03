from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.cli import build_parser
from bildebank.config import AppConfig, BrowserConfig, FaceRecognitionConfig
from bildebank.db import init_database
from bildebank.export_person import (
    PersonExportInterrupted,
    export_person,
    finalize_export_directory,
    validate_windows_folder_name,
)
from bildebank.face import connect_face_db, export_people_browser, export_person_browser, person_source_browser_items
from bildebank.media import sha256_file
from bildebank.server_browser_queries import source_item_ids
from bildebank.server_browser_sources import person_browser_source
from bildebank.target_lock import LOCK_FILENAME
from tests.cli_helpers import capture_cli
from tests.db_test_helpers import register_target_file


class ExportPersonTests(unittest.TestCase):
    def make_collection(self, root: Path) -> tuple[Path, AppConfig, dict[str, int]]:
        target = root / "target"
        init_database(target)
        files = {
            "confirmed": Path("2024/01/same.jpg"),
            "manual": Path("2024/02/same.jpg"),
            "suggested": Path("udatert/suggested.jpg"),
            "hidden": Path("2024/03/hidden.jpg"),
            "motion": Path("2024/04/PXL.mp4"),
            "motion_partner": Path("2024/04/PXL.MP.jpg"),
            "deleted": Path("2024/05/deleted.jpg"),
        }
        ids: dict[str, int] = {}
        for index, (name, relative_path) in enumerate(files.items(), start=1):
            path = target / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"image-{name}".encode())
            os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
            ids[name] = register_target_file(target, relative_path)

        conn = db.connect(target)
        try:
            conn.execute(
                """
                UPDATE files
                SET stored_filename = 'same.jpg',
                    manual_date_from = '2024-01-01',
                    manual_date_to = '2024-01-31'
                WHERE id = ?
                """,
                (ids["manual"],),
            )
            conn.execute(
                "UPDATE files SET taken_date = NULL, date_source = 'none' WHERE id = ?",
                (ids["suggested"],),
            )
            db.tag_file(conn, file_id=ids["hidden"], tag_name=db.SYSTEM_TAG_OUT_OF_FOCUS)
            conn.execute(
                "UPDATE files SET original_filename = 'PXL.MP', stored_filename = 'PXL.mp4' WHERE id = ?",
                (ids["motion"],),
            )
            conn.execute(
                "UPDATE files SET original_filename = 'PXL.MP.jpg' WHERE id = ?",
                (ids["motion_partner"],),
            )
            conn.execute(
                "UPDATE files SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?",
                (ids["deleted"],),
            )
            conn.commit()
        finally:
            conn.close()

        face_conn = connect_face_db(target)
        try:
            face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
            for face_id, name in enumerate(
                ("confirmed", "suggested", "hidden", "motion", "deleted"),
                start=1,
            ):
                file_id = ids[name]
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    ) VALUES(?, ?, ?, 1, 1, 10, 10, 0.9, 'test', ?)
                    """,
                    (face_id, file_id, f"key-{file_id}", f"embedding-{face_id}".encode()),
                )
            face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)")
            face_conn.execute("INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 1, 0.99)")
            face_conn.execute("INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 2, 0.90)")
            face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 3)")
            face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 4)")
            face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 5)")
            face_conn.execute(
                "INSERT INTO person_files(person_id, file_id) VALUES(1, ?)",
                (ids["manual"],),
            )
            face_conn.commit()
        finally:
            face_conn.close()

        config = AppConfig(
            face_recognition=FaceRecognitionConfig(enabled=True),
            browser=BrowserConfig(hide_out_of_focus=True),
        )
        return target, config, ids

    def test_make_person_browser_uses_run_server_person_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, config, ids = self.make_collection(root)

            source = person_browser_source("Kari", include_suggestions=True, show_faces=False)
            server_ids = source_item_ids(target, source, config.face_recognition)
            browser_items = person_source_browser_items(target, "Kari", config.face_recognition)

            output = root / "kari.html"
            output_path = export_person_browser(target, "Kari", output, config=config.face_recognition)
            html = output_path.read_text(encoding="utf-8")
            items_start = html.index("const embeddedItems = ") + len("const embeddedItems = ")
            items_end = html.index(";\n    const MONTH_PREVIEW_LIMIT", items_start)
            html_items = json.loads(html[items_start:items_end])

        self.assertEqual([int(item["fileId"]) for item in browser_items], server_ids)
        self.assertEqual([int(item["fileId"]) for item in html_items], server_ids)
        self.assertIn(ids["confirmed"], server_ids)
        self.assertIn(ids["suggested"], server_ids)
        self.assertIn(ids["manual"], server_ids)
        self.assertIn(ids["hidden"], server_ids)
        self.assertNotIn(ids["motion"], server_ids)
        self.assertNotIn(ids["deleted"], server_ids)
        self.assertIn('"manualDateFrom": "2024-01-01"', html)
        self.assertIn('"dateText": "ca. 2024-01-16 (manuell dato)"', html)
        self.assertNotIn("PXL.mp4", html)
        self.assertNotIn("deleted.jpg", html)

    def test_make_person_browser_can_hide_out_of_focus_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, config, ids = self.make_collection(root)
            source = person_browser_source("Kari", include_suggestions=True, show_faces=False)
            server_ids = source_item_ids(target, source, config.face_recognition, hide_out_of_focus=True)

            output = root / "kari.html"
            output_path = export_person_browser(
                target,
                "Kari",
                output,
                config=config.face_recognition,
                hide_out_of_focus=True,
            )
            html = output_path.read_text(encoding="utf-8")
            items_start = html.index("const embeddedItems = ") + len("const embeddedItems = ")
            items_end = html.index(";\n    const MONTH_PREVIEW_LIMIT", items_start)
            html_items = json.loads(html[items_start:items_end])

            cli_output = root / "kari-cli.html"
            with (
                patch("bildebank.cli.load_config", return_value=config),
                patch("bildebank.cli_face.load_config", return_value=config),
            ):
                code, _stdout, stderr = capture_cli(
                    [
                        "--target",
                        str(target),
                        "make-person-browser",
                        "Kari",
                        "--hide-out-of-focus",
                        "--output",
                        str(cli_output),
                    ]
                )
            cli_html = cli_output.read_text(encoding="utf-8")

        self.assertEqual(code, 0, stderr)
        self.assertEqual([int(item["fileId"]) for item in html_items], server_ids)
        self.assertNotIn(ids["hidden"], server_ids)
        self.assertIn(ids["confirmed"], server_ids)
        self.assertIn(ids["suggested"], server_ids)
        self.assertIn(ids["manual"], server_ids)
        self.assertNotIn("hidden.jpg", html)
        self.assertNotIn("hidden.jpg", cli_html)

    def test_make_people_browser_index_uses_same_visible_items_as_person_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, config, ids = self.make_collection(root)
            main_conn = db.connect(target)
            try:
                rows = list(main_conn.execute("SELECT id, target_path, target_path_key, sha256 FROM files"))
            finally:
                main_conn.close()
            face_conn = connect_face_db(target, config.face_recognition)
            try:
                for row in rows:
                    if int(row["id"]) not in {
                        ids["confirmed"],
                        ids["suggested"],
                        ids["hidden"],
                        ids["motion"],
                        ids["deleted"],
                    }:
                        continue
                    face_conn.execute(
                        """
                        INSERT OR REPLACE INTO scanned_files(
                            file_id, target_path, target_path_key, sha256, status, face_count
                        ) VALUES(?, ?, ?, ?, 'ok', 1)
                        """,
                        (
                            int(row["id"]),
                            str(row["target_path"]),
                            str(row["target_path_key"]),
                            str(row["sha256"]),
                        ),
                    )
                face_conn.commit()
            finally:
                face_conn.close()

            result = export_people_browser(target, config=config.face_recognition, target_locked=True)
            person_html = (target / "person-Kari.html").read_text(encoding="utf-8")
            items_start = person_html.index("const embeddedItems = ") + len("const embeddedItems = ")
            items_end = person_html.index(";\n    const MONTH_PREVIEW_LIMIT", items_start)
            person_items = json.loads(person_html[items_start:items_end])
            index_html = result.index_path.read_text(encoding="utf-8")

            self.assertEqual([int(item["fileId"]) for item in person_items], [ids["confirmed"], ids["hidden"], ids["manual"], ids["suggested"]])
            self.assertIn("4 bilder", index_html)
            self.assertIn("2 bekreftet, 2 forslag", index_html)
            self.assertNotIn("6 bilder", index_html)
            self.assertNotIn("PXL.mp4", person_html)
            self.assertNotIn("deleted.jpg", person_html)

    def test_make_people_browser_can_hide_out_of_focus_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, config, ids = self.make_collection(root)
            with (
                patch("bildebank.cli.load_config", return_value=config),
                patch("bildebank.cli_face.load_config", return_value=config),
            ):
                code, stdout, stderr = capture_cli(
                    [
                        "--target",
                        str(target),
                        "make-people-browser",
                        "--hide-out-of-focus",
                    ]
                )
            person_html = (target / "person-Kari.html").read_text(encoding="utf-8")
            items_start = person_html.index("const embeddedItems = ") + len("const embeddedItems = ")
            items_end = person_html.index(";\n    const MONTH_PREVIEW_LIMIT", items_start)
            person_items = json.loads(person_html[items_start:items_end])
            index_html = (target / "personer.html").read_text(encoding="utf-8")

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev person-index", stdout)
            self.assertEqual(
                [int(item["fileId"]) for item in person_items],
                source_item_ids(
                    target,
                    person_browser_source("Kari", include_suggestions=True, show_faces=False),
                    config.face_recognition,
                    hide_out_of_focus=True,
                ),
            )
            self.assertNotIn(ids["hidden"], [int(item["fileId"]) for item in person_items])
            self.assertNotIn("hidden.jpg", person_html)
            self.assertIn("3 bilder", index_html)
            self.assertIn("1 bekreftet, 2 forslag", index_html)

    def test_export_person_uses_browser_selection_dates_collisions_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, config, ids = self.make_collection(root)
            destination_root = root / "exports"
            destination_root.mkdir()
            conn = db.connect(target)
            try:
                conn.execute(
                    """
                    UPDATE files
                    SET stored_filename = 'foreslått bilde.jpg',
                        manual_date_note = 'Omtrent januar'
                    WHERE id = ?
                    """,
                    (ids["suggested"],),
                )
                conn.execute(
                    "UPDATE files SET view_rotation_degrees = 180 WHERE id = ?",
                    (ids["confirmed"],),
                )
                conn.commit()
            finally:
                conn.close()

            source_hash = sha256_file(target / "2024/01/same.jpg")
            plan = export_person(target, "Kari", destination_root, config=config)

            self.assertEqual(len(plan.entries), 3)
            exported = destination_root / "Kari"
            self.assertEqual((exported / "2024/01/same.jpg").read_bytes(), b"image-confirmed")
            self.assertEqual(sha256_file(exported / "2024/01/same.jpg"), source_hash)
            self.assertEqual((exported / "2024/01/same-1.jpg").read_bytes(), b"image-manual")
            self.assertEqual(
                (exported / "udatert/foreslått bilde.jpg").read_bytes(),
                b"image-suggested",
            )
            self.assertFalse((exported / "2024/03/hidden.jpg").exists())
            self.assertFalse((exported / "2024/04/PXL.mp4").exists())
            self.assertFalse((exported / "2024/05/deleted.jpg").exists())
            source_mtime = (target / "2024/01/same.jpg").stat().st_mtime_ns
            self.assertEqual((exported / "2024/01/same.jpg").stat().st_mtime_ns, source_mtime)
            html = (exported / "index.html").read_text(encoding="utf-8")
            self.assertIn("<title>Kari</title>", html)
            self.assertIn('<nav class="breadcrumb" aria-label="Plassering"><span>År</span></nav>', html)
            self.assertIn("function renderYears()", html)
            self.assertIn('"01": "Januar"', html)
            self.assertIn('"path": "2024/01/same.jpg"', html)
            self.assertIn('"path": "2024/01/same-1.jpg"', html)
            self.assertIn('"url": "udatert/foresl%C3%A5tt%20bilde.jpg"', html)
            self.assertIn('"thumbnailSrc": "udatert/foresl%C3%A5tt%20bilde.jpg"', html)
            self.assertIn('"monthKey": "udatert"', html)
            self.assertIn('"browserDate": "9999-99-99"', html)
            self.assertIn('"manualDateFrom": "2024-01-01"', html)
            self.assertIn('"manualDateTo": "2024-01-31"', html)
            self.assertIn('"dateText": "ca. 2024-01-16 (manuell dato)"', html)
            self.assertIn('"kind": "image"', html)
            self.assertIn('"viewRotation": 180', html)
            self.assertIn('"sizeText":', html)
            self.assertNotIn("hidden.jpg", html)
            self.assertNotIn("PXL.mp4", html)
            self.assertNotIn("deleted.jpg", html)
            self.assertNotIn(str(target), html)
            self.assertFalse((target / LOCK_FILENAME).exists())

    def test_export_person_dry_run_and_cli_output_create_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, config, _ids = self.make_collection(root)
            destination_root = root / "exports"
            destination_root.mkdir()

            plan = export_person(target, "Kari", destination_root, config=config, dry_run=True)

            self.assertEqual(len(plan.entries), 3)
            self.assertFalse((destination_root / "Kari").exists())
            self.assertEqual(list(destination_root.iterdir()), [])

            with (
                patch("bildebank.cli.load_config", return_value=config),
                patch("bildebank.cli_face.load_config", return_value=config),
            ):
                code, stdout, stderr = capture_cli(
                    [
                        "--target",
                        str(target),
                        "export-person",
                        "Kari",
                        "--dest",
                        str(destination_root),
                        "--dry-run",
                    ]
                )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(stdout.count(" -> "), 3)
            self.assertIn("Antall bilder: 3", stdout)
            self.assertNotIn("index.html", stdout)
            self.assertFalse((destination_root / "Kari").exists())

            with (
                patch("bildebank.cli.load_config", return_value=config),
                patch("bildebank.cli_face.load_config", return_value=config),
            ):
                code, stdout, stderr = capture_cli(
                    [
                        "--target",
                        str(target),
                        "export-person",
                        "Kari",
                        "--dest",
                        str(destination_root),
                    ]
                )
            self.assertEqual(code, 0, stderr)
            self.assertIn(f"Statisk browser: {destination_root / 'Kari' / 'index.html'}", stdout)
            self.assertTrue((destination_root / "Kari" / "index.html").is_file())

    def test_export_person_rejects_invalid_inputs_and_keeps_failed_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target, config, _ids = self.make_collection(root)
            destination_root = root / "exports"
            destination_root.mkdir()

            with self.assertRaisesRegex(ValueError, "finnes ikke"):
                export_person(target, "Kari", root / "missing", config=config, dry_run=True)
            with self.assertRaisesRegex(ValueError, "Fant ikke person"):
                export_person(target, "Ukjent", destination_root, config=config, dry_run=True)
            empty_face_conn = connect_face_db(target)
            try:
                empty_face_conn.execute("INSERT INTO persons(name) VALUES('Tom')")
                empty_face_conn.commit()
            finally:
                empty_face_conn.close()
            with self.assertRaisesRegex(ValueError, "ingen synlige bilder"):
                export_person(target, "Tom", destination_root, config=config, dry_run=True)
            self.assertFalse((destination_root / "Tom").exists())
            with self.assertRaisesRegex(ValueError, "overlappe"):
                export_person(target, "Kari", target, config=config, dry_run=True)
            (destination_root / "Kari").mkdir()
            with self.assertRaisesRegex(ValueError, "finnes allerede"):
                export_person(target, "Kari", destination_root, config=config, dry_run=True)
            with self.assertRaisesRegex(ValueError, "Windows-mappenavn"):
                validate_windows_folder_name("Kari.")
            with self.assertRaisesRegex(ValueError, "reservert"):
                validate_windows_folder_name("CON.txt")

            (destination_root / "Kari").rmdir()
            with patch("bildebank.export_person.safe_copy", side_effect=OSError("kopifeil")):
                with self.assertRaisesRegex(RuntimeError, "Ufullstendig eksport er beholdt"):
                    export_person(target, "Kari", destination_root, config=config)
            self.assertFalse((destination_root / "Kari").exists())
            incomplete = list(destination_root.glob(".bildebank-export-person-Kari-incomplete-*"))
            self.assertEqual(len(incomplete), 1)

            incomplete[0].rename(root / "failed-export")
            with patch("bildebank.export_person.write_export_browser", side_effect=OSError("skrivefeil")):
                with self.assertRaisesRegex(RuntimeError, "Ufullstendig eksport er beholdt"):
                    export_person(target, "Kari", destination_root, config=config)
            self.assertFalse((destination_root / "Kari").exists())
            html_incomplete = list(destination_root.glob(".bildebank-export-person-Kari-incomplete-*"))
            self.assertEqual(len(html_incomplete), 1)
            self.assertEqual(len(list(html_incomplete[0].rglob("*.jpg"))), 3)

            html_incomplete[0].rename(root / "failed-html-export")
            with patch("bildebank.export_person.safe_copy", side_effect=KeyboardInterrupt):
                with self.assertRaisesRegex(PersonExportInterrupted, "Ufullstendig eksport er beholdt"):
                    export_person(target, "Kari", destination_root, config=config)
            self.assertFalse((destination_root / "Kari").exists())

    def test_export_person_retries_transient_windows_directory_rename_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            temporary = root / ".incomplete"
            destination = root / "Anders"
            temporary.mkdir()
            real_rename = Path.rename
            attempts = 0

            def transient_rename(path: Path, target: Path) -> Path:
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    error = PermissionError(13, "Ingen tilgang", str(path), 5)
                    error.winerror = 5
                    raise error
                return real_rename(path, target)

            with (
                patch("pathlib.Path.rename", transient_rename),
                patch("bildebank.export_person.time.sleep") as sleep,
            ):
                finalize_export_directory(temporary, destination)

            self.assertEqual(attempts, 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertTrue(destination.is_dir())
            self.assertFalse(temporary.exists())

    def test_export_person_parser_help_and_reference(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["export-person", "Kari", "--dest", r"D:\Eksport", "--dry-run"])
        self.assertEqual(args.command, "export-person")
        self.assertEqual(args.name, "Kari")
        self.assertTrue(args.dry_run)
        self.assertIn("export-person", parser.format_help())
        reference = Path("docs/reference.md").read_text(encoding="utf-8")
        self.assertIn("[`export-person`](export-person.md)", reference)
