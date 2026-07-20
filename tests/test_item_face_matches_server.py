from __future__ import annotations

import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from bildebank.config import AppConfig, FaceRecognitionConfig
from bildebank.face import connect_face_db
from bildebank.media import ImageDimensions
from bildebank.server_assets import SERVER_JS
from bildebank.server_browser_queries import browser_item_by_id
from bildebank.server_faces import item_face_matches_content_html
from bildebank.server_handler import BildebankRequestHandler
from bildebank.server_pages import item_page_html
from tests.cli_helpers import run_cli
from tests.test_media import minimal_png


class ItemFaceMatchesServerTests(unittest.TestCase):
    def make_target(self, root: Path) -> tuple[Path, object]:
        target = root / "target"
        source = root / "source"
        source.mkdir()
        (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))
        (source / "IMG_20240103.png").write_bytes(minimal_png(100, 80))
        self.assertEqual(run_cli(["create", str(target)]), 0)
        self.assertEqual(
            run_cli(
                [
                    "--target",
                    str(target),
                    "import",
                    "--name",
                    source.name,
                    "--quiet",
                    str(source),
                ]
            ),
            0,
        )
        item = browser_item_by_id(target, 1)
        self.assertIsNotNone(item)
        return target, item

    @staticmethod
    def embedding(x: float, y: float) -> bytes:
        return np.asarray([x, y], dtype=np.float32).tobytes()

    def add_face(
        self,
        conn: object,
        face_id: int,
        file_id: int,
        embedding: bytes,
        *,
        x: float = 10,
        y: float = 20,
        width: float = 30,
        height: float = 10,
    ) -> None:
        conn.execute(  # type: ignore[attr-defined]
            """
            INSERT INTO faces(
                id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                detection_score, embedding_model, embedding
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 0.9, 'test', ?)
            """,
            (face_id, file_id, f"key-{face_id}", x, y, width, height, embedding),
        )

    def test_best_reference_per_person_top_three_sorting_and_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target, item = self.make_target(Path(tmp))
            conn = connect_face_db(target)
            try:
                self.add_face(conn, 1, 1, self.embedding(1, 0))
                people = [(1, "Ada"), (2, "Bea"), (3, "Cato"), (4, "Dag"), (5, "Negativ")]
                conn.executemany("INSERT INTO persons(id, name) VALUES(?, ?)", people)
                references = [
                    (2, 2, self.embedding(0.5369, 0.843646)),
                    (3, 2, self.embedding(0.4, 0.916515)),
                    (4, 2, self.embedding(0.5369, 0.843646)),
                    (5, 2, self.embedding(0.5, 0.866025)),
                    (6, 2, self.embedding(0.3, 0.953939)),
                    (7, 2, self.embedding(-1, 0)),
                ]
                for face_id, file_id, embedding in references:
                    self.add_face(conn, face_id, file_id, embedding)
                conn.executemany(
                    "INSERT INTO person_faces(person_id, face_id) VALUES(?, ?)",
                    [(1, 2), (1, 3), (2, 4), (3, 5), (4, 6), (5, 7)],
                )
                conn.commit()
            finally:
                conn.close()

            body = item_face_matches_content_html(target, item)

        self.assertEqual(body.count("<li>"), 3)
        self.assertLess(body.index(">Ada</a>"), body.index(">Bea</a>"))
        self.assertLess(body.index(">Bea</a>"), body.index(">Cato</a>"))
        self.assertIn("threshold ≤ 0,536", body)
        self.assertIn('/person/Ada/confirmed/item/2', body)
        self.assertNotIn("Dag", body)
        self.assertNotIn("Negativ", body)
        self.assertIn("Hver person vurderes uavhengig", body)
        self.assertIn("bare det beste treffet totalt", body)

    def test_target_face_excludes_itself_but_uses_other_reference_and_renders_all_faces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target, item = self.make_target(Path(tmp))
            item = dict(item)
            item["view_rotation_degrees"] = 90
            conn = connect_face_db(target)
            try:
                conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                self.add_face(conn, 1, 1, self.embedding(1, 0))
                self.add_face(conn, 2, 1, self.embedding(0, -1), x=5, y=6)
                self.add_face(conn, 3, 2, self.embedding(0.8, 0.6))
                conn.executemany(
                    "INSERT INTO person_faces(person_id, face_id) VALUES(1, ?)",
                    [(1,), (3,)],
                )
                conn.commit()
            finally:
                conn.close()

            with patch(
                "bildebank.server_faces.cached_face_box_media_metadata",
                return_value=(ImageDimensions(100, 80), 6),
            ):
                body = item_face_matches_content_html(target, item, read_only=True)

        self.assertEqual(body.count('data-face-match-detail="'), 2)
        self.assertIn('/person/Kari/confirmed/item/2', body)
        self.assertIn("left: 62.5000%; top: 10.0000%; width: 12.5000%; height: 30.0000%", body)
        self.assertIn('data-view-rotation="90"', body)
        self.assertIn("Ingen bekreftede ansikter gir et mulig treff", body)

    @patch("bildebank.server_browser_item_html.motion_video_for_image", return_value=None)
    @patch("bildebank.server_browser_item_html.raw_sidecar_for_image", return_value=None)
    @patch("bildebank.server_browser_item_html._source_item_header_html", side_effect=lambda *args, **kwargs: args[3])
    @patch("bildebank.server_browser_item_html._source_item_side_panel_html", return_value="")
    @patch("bildebank.server_browser_item_html._source_item_face_html", return_value=("", "", "", False))
    def test_item_button_is_lazy_and_available_read_only(
        self, _faces: Mock, _side: Mock, _header: Mock, _raw: Mock, _motion: Mock
    ) -> None:
        item = {"id": 7, "target_path": "2020/01/image.jpg", "stored_filename": "image.jpg"}
        nav = {key: None for key in ("previous_year", "next_year", "previous_month", "next_month")}
        with patch("bildebank.server_faces.item_face_matches_content_html") as calculate:
            body = item_page_html(Path("target"), item, None, None, nav, face_enabled=True)
            read_only = item_page_html(
                Path("target"), item, None, None, nav, face_enabled=True, read_only=True
            )
            disabled = item_page_html(Path("target"), item, None, None, nav, face_enabled=False)
        calculate.assert_not_called()
        self.assertIn('data-face-matches-item="7"', body)
        self.assertIn('data-face-matches-item="7"', read_only)
        self.assertNotIn("data-open-face-matches", disabled)
        self.assertNotIn("faceMatchesOverlay", body)
        self.assertIn("document.createElement(\"div\")", SERVER_JS)
        self.assertIn("Beregner …", SERVER_JS)
        self.assertIn("/api/item-face-matches?file_id=", SERVER_JS)
        self.assertIn("faceMatchesLoaded = true", SERVER_JS)

    def test_api_errors_and_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target, _item = self.make_target(Path(tmp))
            responses: list[tuple[dict[str, object], HTTPStatus]] = []
            handler = object.__new__(BildebankRequestHandler)
            handler.server = SimpleNamespace(
                target=target,
                face_enabled=True,
                read_only=True,
                config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=True)),
            )
            handler.respond_json = lambda content, status=HTTPStatus.OK: responses.append((content, status))
            handler.respond_item_face_matches("file_id=1")
            handler.respond_item_face_matches("file_id=nope")
            handler.respond_item_face_matches("file_id=999")
            handler.server.face_enabled = False
            handler.respond_item_face_matches("file_id=1")

        self.assertEqual(responses[0][1], HTTPStatus.OK)
        self.assertIn("ingen scannede ansikter", str(responses[0][0]["html"]).lower())
        self.assertEqual(responses[1][1], HTTPStatus.BAD_REQUEST)
        self.assertEqual(responses[2][1], HTTPStatus.NOT_FOUND)
        self.assertEqual(responses[3][1], HTTPStatus.FORBIDDEN)


if __name__ == "__main__":
    unittest.main()
