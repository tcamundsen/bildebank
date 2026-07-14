from __future__ import annotations

import tempfile
import unittest
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bildebank import db
from bildebank.config import (
    AppConfig,
    FaceRecognitionConfig,
    load_config,
    set_face_suggest_threshold,
)
from bildebank.face import FaceSuggestStats, connect_face_db
from bildebank.server import BildebankRequestHandler
from bildebank.server_assets import SERVER_JS
from bildebank.server_browser_queries import source_item_ids
from bildebank.server_browser_sources import missing_face_suggestions_browser_source
from bildebank.server_pages import people_page_html
from bildebank.server_pages import item_page_html


class FaceSuggestServerTests(unittest.TestCase):
    def test_config_defaults_loads_and_updates_without_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(load_config(root).face_recognition.suggest_threshold, 0.6)
            (root / "bildebank-config.toml").write_text(
                "[face_recognition]\nenabled = true\nmodel_name = \"buffalo_s\"\nsuggest_threshold = 0.72\n",
                encoding="utf-8",
            )
            self.assertEqual(load_config(root).face_recognition.suggest_threshold, 0.72)

            set_face_suggest_threshold(root, 0.55)

            config = load_config(root).face_recognition
            self.assertTrue(config.enabled)
            self.assertEqual(config.model_name, "buffalo_s")
            self.assertEqual(config.suggest_threshold, 0.55)

    @patch("bildebank.server_faces.registered_people_rows", return_value=[])
    @patch("bildebank.server_faces.people_face_summary")
    def test_people_page_contains_modal_and_saved_threshold(self, summary: Mock, _rows: Mock) -> None:
        summary.return_value = SimpleNamespace()
        with patch("bildebank.server_faces.people_face_summary_html", return_value=""):
            body = people_page_html(
                Path("."),
                FaceRecognitionConfig(enabled=True, suggest_threshold=0.725),
            )
            read_only_body = people_page_html(
                Path("."),
                FaceRecognitionConfig(enabled=True, suggest_threshold=0.725),
                read_only=True,
            )
        self.assertIn("Foreslå personer", body)
        self.assertIn('href="/people/missing-suggestions"', body)
        self.assertIn('action="/people/face-suggest"', body)
        self.assertIn('name="return_url" value="/people"', body)
        self.assertIn('value="0.725"', body)
        self.assertNotIn("data-confirm-submit=", body)
        self.assertIn("data-close-face-suggest", body)
        self.assertIn("Face-suggest fullført", body)
        self.assertIn("data-face-suggest-success", body)
        self.assertIn("data-face-suggest-status", body)
        self.assertIn("Klikk knappen 'Finn ansikter'", body)
        self.assertEqual(body.count("data-open-face-suggest"), 2)

        self.assertNotIn('action="/people/face-suggest"', read_only_body)
        self.assertNotIn('href="/people/missing-suggestions"', read_only_body)
        self.assertNotIn("data-open-face-suggest", read_only_body)
        self.assertNotIn("Klikk knappen 'Finn ansikter'", read_only_body)
        self.assertNotIn("personRenameDialog", read_only_body)

    def make_handler(self, *, enabled: bool, threshold: str = "0.65", return_url: str = ""):
        data = f"threshold={threshold}&return_url={return_url}".encode()
        handler = object.__new__(BildebankRequestHandler)
        handler.headers = {"Content-Length": str(len(data))}
        handler.rfile = BytesIO(data)
        handler.server = SimpleNamespace(
            target=Path("target"),
            config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=enabled)),
            face_enabled=enabled,
            openclip_enabled=False,
            clear_browser_navigation_cache=Mock(),
        )
        handler.response = None
        handler.redirect_location = None

        def respond_html(content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
            handler.response = (content, status)

        handler.respond_html = respond_html
        handler.redirect = lambda location: setattr(handler, "redirect_location", location)
        return handler

    @patch("bildebank.server_browser_item_html.motion_video_for_image", return_value=None)
    @patch("bildebank.server_browser_item_html.raw_sidecar_for_image", return_value=None)
    @patch("bildebank.server_browser_item_html._source_item_header_html", side_effect=lambda *args, **kwargs: args[3])
    @patch("bildebank.server_browser_item_html._source_item_side_panel_html", return_value="")
    @patch("bildebank.server_browser_item_html._source_item_face_html", return_value=("", "", "", False))
    def test_browser_toolbar_contains_face_suggest_modal_and_return_url(
        self,
        _faces: Mock,
        _tags: Mock,
        _header: Mock,
        _raw_sidecar: Mock,
        _motion: Mock,
    ) -> None:
        item = {"id": 7, "target_path": "target/2020/01/image.jpg", "stored_filename": "image.jpg"}
        month_nav = {
            "previous_year": None,
            "next_year": None,
            "previous_month": None,
            "next_month": None,
        }
        body = item_page_html(
            Path("target"),
            item,
            None,
            None,
            month_nav,
            face_enabled=True,
            face_config=FaceRecognitionConfig(enabled=True, suggest_threshold=0.725),
        )
        self.assertIn("👥✨", body)
        self.assertIn('title="Kjør face-suggest for å finne ansikter"', body)
        self.assertIn('data-open-manual-date', body)
        self.assertIn('data-rotate-item="7"', body)
        self.assertIn('data-delete-item="7"', body)
        self.assertIn('action="/people/face-suggest"', body)
        self.assertIn('value="0.725"', body)
        self.assertIn('name="return_url" value="/item/7"', body)

        read_only_body = item_page_html(
            Path("target"),
            item,
            None,
            None,
            month_nav,
            face_enabled=True,
            face_config=FaceRecognitionConfig(enabled=True, suggest_threshold=0.725),
            read_only=True,
        )
        self.assertNotIn("👥✨", read_only_body)
        self.assertNotIn("faceSuggestDialog", read_only_body)
        self.assertNotIn('data-open-manual-date', read_only_body)
        self.assertNotIn('data-rotate-item="7"', read_only_body)
        self.assertNotIn('data-delete-item="7"', read_only_body)

        disabled_body = item_page_html(
            Path("target"),
            item,
            None,
            None,
            month_nav,
            face_enabled=False,
        )
        self.assertNotIn("👥✨", disabled_body)
        self.assertNotIn("faceSuggestDialog", disabled_body)

    def test_browser_script_opens_completed_dialog_and_changes_close_label(self) -> None:
        self.assertIn('get("face-suggest-status")', SERVER_JS)
        self.assertIn("faceSuggestSuccess.hidden = false", SERVER_JS)
        self.assertIn('closeFaceSuggestButton.textContent = "Lukk"', SERVER_JS)
        self.assertIn("faceSuggestDialog.hidden = false", SERVER_JS)
        self.assertIn("history.replaceState", SERVER_JS)
        self.assertIn("faceSuggestSuccess.hidden = true", SERVER_JS)
        self.assertIn('faceSuggestStatus.textContent = ""', SERVER_JS)
        self.assertIn('closeFaceSuggestButton.textContent = "Avbryt"', SERVER_JS)

    @patch("bildebank.server_endpoints_faces.set_face_suggest_threshold")
    @patch("bildebank.server_endpoints_faces.suggest_faces")
    def test_disabled_and_invalid_threshold_do_not_run(self, suggest: Mock, save: Mock) -> None:
        disabled = self.make_handler(enabled=False)
        disabled.respond_face_suggest()
        self.assertEqual(disabled.response[1], HTTPStatus.FORBIDDEN)

        invalid = self.make_handler(enabled=True, threshold="nan")
        invalid.respond_face_suggest()
        self.assertEqual(invalid.response[1], HTTPStatus.BAD_REQUEST)
        self.assertIn("endelig tall", invalid.response[0])
        suggest.assert_not_called()
        save.assert_not_called()

    @patch("bildebank.server_endpoints_faces.people_page_html", return_value="people-result")
    @patch("bildebank.server_endpoints_faces.clear_face_caches")
    @patch("bildebank.server_endpoints_faces.suggest_faces")
    @patch("bildebank.server_endpoints_faces.set_face_suggest_threshold")
    def test_success_updates_config_caches_and_status(
        self,
        save: Mock,
        suggest: Mock,
        clear_faces: Mock,
        render: Mock,
    ) -> None:
        suggest.return_value = FaceSuggestStats(2, 8, 5, 0.65)
        handler = self.make_handler(enabled=True)

        handler.respond_face_suggest()

        self.assertEqual(handler.response, ("people-result", HTTPStatus.OK))
        save.assert_called_once()
        passed_config = suggest.call_args.kwargs["config"]
        self.assertEqual(suggest.call_args.kwargs["threshold"], 0.65)
        self.assertEqual(passed_config.suggest_threshold, 0.65)
        self.assertEqual(handler.server.config.face_recognition.suggest_threshold, 0.65)
        clear_faces.assert_called_once_with()
        handler.server.clear_browser_navigation_cache.assert_called_once_with()
        self.assertIn(
            "Ansiktsforslag: personer=2, ukjente_ansikter=8, forslag=5, threshold=0.650",
            render.call_args.kwargs["message"],
        )

    @patch("bildebank.server_endpoints_faces.people_page_html")
    @patch("bildebank.server_endpoints_faces.clear_face_caches")
    @patch("bildebank.server_endpoints_faces.suggest_faces", return_value=FaceSuggestStats(2, 8, 5, 0.65))
    @patch("bildebank.server_endpoints_faces.set_face_suggest_threshold")
    def test_success_redirects_only_to_local_return_url(
        self,
        _save: Mock,
        _suggest: Mock,
        _clear_faces: Mock,
        render: Mock,
    ) -> None:
        handler = self.make_handler(enabled=True, return_url="%2Fperson%2FKari%2Fitem%2F7")
        handler.respond_face_suggest()
        self.assertEqual(
            handler.redirect_location,
            "/person/Kari/item/7#face-suggest-status=Ansiktsforslag%3A+personer%3D2%2C+"
            "ukjente_ansikter%3D8%2C+forslag%3D5%2C+threshold%3D0.650",
        )
        handler.server.clear_browser_navigation_cache.assert_called_once_with()
        render.assert_not_called()

        handler = self.make_handler(enabled=True, return_url="%2Fpeople")
        handler.respond_face_suggest()
        self.assertTrue(handler.redirect_location.startswith("/people#face-suggest-status="))
        render.assert_not_called()

        for return_url in ("https%3A%2F%2Fevil.example%2F", "%2F%2Fevil.example%2F", "item%2F7"):
            handler = self.make_handler(enabled=True, return_url=return_url)
            handler.respond_face_suggest()
            self.assertIsNone(handler.redirect_location)
            self.assertEqual(handler.response, (render.return_value, HTTPStatus.OK))

    @patch("bildebank.server_endpoints_faces.set_face_suggest_threshold")
    @patch(
        "bildebank.server_endpoints_faces.suggest_faces",
        side_effect=ValueError("Face-database finnes ikke. Kjør bildebank face-scan først."),
    )
    def test_missing_face_database_is_readable_html_error(self, _suggest: Mock, save: Mock) -> None:
        handler = self.make_handler(enabled=True)
        handler.respond_face_suggest()
        self.assertEqual(handler.response[1], HTTPStatus.BAD_REQUEST)
        self.assertIn("Kjør bildebank face-scan først.", handler.response[0])
        save.assert_called_once()

    def test_missing_face_suggestions_source_selects_unconfirmed_faces_without_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            db.init_database(target)
            main_conn = db.connect(target)
            try:
                for file_id, name in ((1, "missing.jpg"), (2, "suggested.jpg"), (3, "confirmed.jpg")):
                    relative_path = Path("2024/01") / name
                    (target / relative_path).parent.mkdir(parents=True, exist_ok=True)
                    (target / relative_path).write_bytes(b"image")
                    main_conn.execute(
                        """
                        INSERT INTO files(
                            id, target_path, target_path_key, original_filename, stored_filename,
                            sha256, size_bytes, taken_date, date_source
                        )
                        VALUES(?, ?, ?, ?, ?, ?, 5, '2024-01-01', 'metadata')
                        """,
                        (
                            file_id,
                            relative_path.as_posix(),
                            relative_path.as_posix(),
                            name,
                            name,
                            f"sha-{file_id}",
                        ),
                    )
                main_conn.commit()
            finally:
                main_conn.close()

            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                for face_id, file_id in ((1, 1), (2, 2), (3, 3), (4, 1)):
                    face_conn.execute(
                        """
                        INSERT INTO faces(
                            id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                            detection_score, embedding_model, embedding
                        )
                        VALUES(?, ?, ?, 1, 2, 10, 20, 0.9, 'test', ?)
                        """,
                        (face_id, file_id, f"face-{face_id}", f"embedding-{face_id}".encode()),
                    )
                face_conn.execute("INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 2, 0.9)")
                face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 3)")
                face_conn.execute("INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 4, 0.8)")
                face_conn.commit()
            finally:
                face_conn.close()

            self.assertEqual(source_item_ids(target, missing_face_suggestions_browser_source()), [1])

    def test_get_route_dispatches_missing_face_suggestions_browser(self) -> None:
        handler = object.__new__(BildebankRequestHandler)
        handler.path = "/people/missing-suggestions/item/7"
        handler.server = SimpleNamespace(face_enabled=True)
        handler.respond_missing_face_suggestions = Mock()

        handler.do_GET()

        handler.respond_missing_face_suggestions.assert_called_once_with("/item/7")

    def test_missing_face_suggestions_handler_accepts_item_path(self) -> None:
        handler = object.__new__(BildebankRequestHandler)
        handler.server = SimpleNamespace(
            target=Path("target"),
            config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=True)),
            hide_out_of_focus=False,
        )
        handler.respond_text = Mock()
        handler.respond_browser_source = Mock()

        handler.respond_missing_face_suggestions("/item/7")

        handler.respond_text.assert_not_called()
        source, page_mode, raw_value = handler.respond_browser_source.call_args.args[:3]
        self.assertEqual(source.root_url, "/people/missing-suggestions")
        self.assertEqual(page_mode, "item")
        self.assertEqual(raw_value, "7")


if __name__ == "__main__":
    unittest.main()
