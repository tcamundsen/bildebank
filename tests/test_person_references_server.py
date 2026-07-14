from __future__ import annotations

import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bildebank import db
from bildebank.config import AppConfig, FaceRecognitionConfig
from bildebank.face import connect_face_db
from bildebank.server_handler import BildebankRequestHandler
from bildebank.server_faces import (
    people_page_html,
    person_reference_items,
    person_references_page_html,
)
from bildebank.server_browser_queries import source_item_ids
from bildebank.server_browser_sources import person_reference_suggestions_browser_source
from bildebank.server_pages import shell_page_html


class PersonReferencesServerTests(unittest.TestCase):
    def make_target(self, root: Path) -> Path:
        target = root / "target"
        db.init_database(target)
        main_conn = db.connect(target)
        try:
            for file_id, name, deleted_at in (
                (1, "one.jpg", None),
                (2, "two.jpg", None),
                (3, "deleted.jpg", "2026-01-01 00:00:00"),
            ):
                relative_path = Path("2024/01") / name
                absolute_path = target / relative_path
                absolute_path.parent.mkdir(parents=True, exist_ok=True)
                absolute_path.write_bytes(b"image")
                main_conn.execute(
                    """
                    INSERT INTO files(
                        id, target_path, target_path_key, original_filename, stored_filename,
                        sha256, size_bytes, taken_date, date_source, deleted_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, 5, '2024-01-01', 'metadata', ?)
                    """,
                    (
                        file_id,
                        relative_path.as_posix(),
                        relative_path.as_posix(),
                        name,
                        name,
                        f"sha-{file_id}",
                        deleted_at,
                    ),
                )
            out_of_focus_tag_id = int(
                main_conn.execute(
                    "SELECT id FROM tags WHERE name_key = ?",
                    (db.normalize_tag_name(db.SYSTEM_TAG_OUT_OF_FOCUS).casefold(),),
                ).fetchone()[0]
            )
            main_conn.execute("INSERT INTO file_tags(file_id, tag_id) VALUES(2, ?)", (out_of_focus_tag_id,))
            main_conn.commit()
        finally:
            main_conn.close()

        face_conn = connect_face_db(target)
        try:
            face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari / Åse')")
            face_conn.execute("INSERT INTO persons(id, name) VALUES(2, 'Ola')")
            for face_id, file_id in (
                (1, 1),
                (2, 1),
                (3, 2),
                (4, 3),
                (10, 1),
                (11, 2),
                (12, 2),
                (13, 2),
                (14, 2),
                (15, 3),
            ):
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
                face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, ?)", (face_id,))
            for suggestion_face_id, person_id, reference_face_id in (
                (10, 1, 1),
                (11, 1, 1),
                (12, 1, 2),
                (13, 1, None),
                (14, 2, 1),
                (15, 1, 4),
            ):
                face_conn.execute(
                    """
                    INSERT INTO face_suggestions(person_id, face_id, reference_face_id, similarity)
                    VALUES(?, ?, ?, 0.9)
                    """,
                    (person_id, suggestion_face_id, reference_face_id),
                )
            face_conn.commit()
        finally:
            face_conn.close()
        return target

    def test_reference_items_group_counts_and_filter_deleted_but_not_out_of_focus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = self.make_target(Path(tmp))

            references = person_reference_items(target, "Kari / Åse")

            self.assertEqual([(int(item["id"]), count) for item, count in references], [(1, 3), (2, 0)])

    def test_people_link_and_reference_page_use_encoded_name_confirmed_browser_and_empty_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = self.make_target(Path(tmp))

            people_body = people_page_html(target, shell_page_html=shell_page_html)
            reference_body = person_references_page_html(
                target,
                "Kari / Åse",
                shell_page_html=shell_page_html,
            )

            self.assertIn('href="/people/Kari%20%2F%20%C3%85se/references"', people_body)
            self.assertIn("Referansebilder (2)", people_body)
            self.assertEqual(reference_body.count('<article class="item">'), 2)
            self.assertIn('href="/person/Kari%20%2F%20%C3%85se/confirmed/item/1"', reference_body)
            self.assertIn(
                'href="/people/Kari%20%2F%20%C3%85se/references/1">Foreslåtte bilder: 3</a>',
                reference_body,
            )
            self.assertIn("Foreslåtte bilder: 0", reference_body)
            self.assertNotIn('href="/people/Kari%20%2F%20%C3%85se/references/2">Foreslåtte bilder: 0</a>', reference_body)

            empty_face_conn = connect_face_db(target)
            try:
                empty_face_conn.execute("INSERT INTO persons(id, name) VALUES(3, 'Tom')")
                empty_face_conn.commit()
            finally:
                empty_face_conn.close()
            empty_body = person_references_page_html(target, "Tom", shell_page_html=shell_page_html)
            self.assertIn("Ingen referansebilder for denne personen ennå.", empty_body)

    def test_reference_suggestions_browser_filters_by_reference_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = self.make_target(Path(tmp))

            source = person_reference_suggestions_browser_source("Kari / Åse", 1)

            self.assertEqual([1, 2], source_item_ids(target, source, FaceRecognitionConfig(enabled=True)))

    def test_reference_handler_decodes_name_and_uses_existing_not_found_page(self) -> None:
        handler = object.__new__(BildebankRequestHandler)
        handler.server = SimpleNamespace(
            target=Path("target"),
            config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=True)),
            face_enabled=True,
            openclip_enabled=False,
        )
        handler.response = None
        handler.respond_html = lambda content, status=HTTPStatus.OK: setattr(handler, "response", (content, status))

        with (
            patch("bildebank.server_endpoints_faces.person_by_name", return_value={"name": "Kari / Åse"}) as find_person,
            patch("bildebank.server_endpoints_faces.person_references_page_html", return_value="references") as render,
        ):
            handler.respond_person_references("Kari%20%2F%20%C3%85se")
        find_person.assert_called_once_with(handler.server.target, "Kari / Åse", handler.server.config.face_recognition)
        render.assert_called_once()
        self.assertEqual(handler.response, ("references", HTTPStatus.OK))

        with (
            patch("bildebank.server_endpoints_faces.person_by_name", return_value=None),
            patch("bildebank.server_endpoints_faces.person_not_found_html", return_value="not-found"),
        ):
            handler.respond_person_references("Ukjent")
        self.assertEqual(handler.response, ("not-found", HTTPStatus.NOT_FOUND))

    def test_get_route_dispatches_to_reference_handler(self) -> None:
        handler = object.__new__(BildebankRequestHandler)
        handler.path = "/people/Kari%20%2F%20%C3%85se/references"
        handler.server = SimpleNamespace(face_enabled=True)
        handler.respond_person_references = Mock()

        handler.do_GET()

        handler.respond_person_references.assert_called_once_with("Kari%20%2F%20%C3%85se")

    def test_get_route_dispatches_reference_suggestions_to_browser_handler(self) -> None:
        handler = object.__new__(BildebankRequestHandler)
        handler.path = "/people/Kari%20%2F%20%C3%85se/references/1/item/2"
        handler.server = SimpleNamespace(face_enabled=True)
        handler.respond_person_reference_suggestions = Mock()

        handler.do_GET()

        handler.respond_person_reference_suggestions.assert_called_once_with("Kari%20%2F%20%C3%85se/references/1/item/2")


if __name__ == "__main__":
    unittest.main()
