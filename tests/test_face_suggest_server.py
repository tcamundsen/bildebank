from __future__ import annotations

import tempfile
import unittest
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bildebank.config import (
    AppConfig,
    FaceRecognitionConfig,
    load_config,
    set_face_suggest_threshold,
)
from bildebank.face import FaceSuggestStats
from bildebank.server import BildebankRequestHandler
from bildebank.server_assets import SERVER_JS
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
        self.assertIn("Foreslå personer", body)
        self.assertIn('action="/people/face-suggest"', body)
        self.assertIn('name="return_url" value="/people"', body)
        self.assertIn('value="0.725"', body)
        self.assertNotIn("data-confirm-submit=", body)
        self.assertIn("data-close-face-suggest", body)
        self.assertIn("Face-suggest fullført", body)
        self.assertIn("data-face-suggest-success", body)
        self.assertIn("data-face-suggest-status", body)

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

    @patch("bildebank.server_browser.motion_video_for_image", return_value=None)
    @patch("bildebank.server_browser.all_browser_item_link_url", return_value="/item/7")
    @patch("bildebank.server_browser._source_item_header_html", side_effect=lambda *args, **kwargs: args[3])
    @patch("bildebank.server_browser._source_item_tag_controls_html", return_value="")
    @patch("bildebank.server_browser._source_item_face_html", return_value=("", "", "", False))
    def test_browser_toolbar_contains_face_suggest_modal_and_return_url(
        self,
        _faces: Mock,
        _tags: Mock,
        _header: Mock,
        _all_items: Mock,
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
        self.assertIn('action="/people/face-suggest"', body)
        self.assertIn('value="0.725"', body)
        self.assertIn('name="return_url" value="/item/7"', body)

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

    @patch("bildebank.server.set_face_suggest_threshold")
    @patch("bildebank.server.suggest_faces")
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

    @patch("bildebank.server.people_page_html", return_value="people-result")
    @patch("bildebank.server.clear_face_caches")
    @patch("bildebank.server.suggest_faces")
    @patch("bildebank.server.set_face_suggest_threshold")
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

    @patch("bildebank.server.people_page_html")
    @patch("bildebank.server.clear_face_caches")
    @patch("bildebank.server.suggest_faces", return_value=FaceSuggestStats(2, 8, 5, 0.65))
    @patch("bildebank.server.set_face_suggest_threshold")
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

    @patch("bildebank.server.set_face_suggest_threshold")
    @patch("bildebank.server.suggest_faces", side_effect=ValueError("Face-database finnes ikke. Kjør bildebank face-scan først."))
    def test_missing_face_database_is_readable_html_error(self, _suggest: Mock, save: Mock) -> None:
        handler = self.make_handler(enabled=True)
        handler.respond_face_suggest()
        self.assertEqual(handler.response[1], HTTPStatus.BAD_REQUEST)
        self.assertIn("Kjør bildebank face-scan først.", handler.response[0])
        save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
