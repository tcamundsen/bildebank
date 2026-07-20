from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bildebank import db, server_endpoints_browser, server_endpoints_faces
from bildebank.config import AppConfig, FaceRecognitionConfig, OpenClipConfig
from bildebank.face import add_person_to_file, connect_face_db, remove_person_from_file
from bildebank.media_cache import cached_image_dimensions, cached_image_orientation
from bildebank.server_handler import BildebankRequestHandler
from bildebank.server_runtime import BildebankServer
from bildebank.server_assets import SERVER_CSS, SERVER_JS
from bildebank.server_browser_queries import (
    adjacent_browser_items,
    adjacent_person_items,
    adjacent_source_items,
    browser_item_by_id,
    browser_month_navigation,
    person_item_by_id,
    person_month_items,
    person_month_navigation,
    source_item_by_id,
    source_item_count,
    source_item_ids,
    source_month_items,
    source_month_keys,
    source_month_navigation,
)
from bildebank.server_browser_sources import (
    person_browser_source,
    source_has_sql_filter,
)
from bildebank.server_faces import (
    cached_person_file_ids,
    clear_face_caches,
    face_overlay_content_html,
    people_for_file,
    person_file_ids,
    person_items,
)
from bildebank.server_pages import (
    item_page_html,
    people_page_html,
    person_item_page_html,
    person_month_page_html,
    source_item_page_html,
    source_month_page_html,
    source_years_page_html,
)
from bildebank.target_lock import LOCK_FILENAME
from tests.cli_helpers import run_cli
from tests.test_media import minimal_png


class ServerPeopleCliTests(unittest.TestCase):
    def test_run_server_item_page_links_known_and_suggested_people(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

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
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.execute(
                    "INSERT INTO persons(id, name) VALUES(2, 'Ola Nordmann')"
                )
                face_conn.execute(
                    "INSERT INTO persons(id, name) VALUES(3, 'Per Manual')"
                )
                face_conn.execute("INSERT INTO persons(id, name) VALUES(4, 'Siril')")
                face_conn.execute("INSERT INTO persons(id, name) VALUES(5, 'Viljar')")
                face_conn.execute(
                    "INSERT INTO persons(id, name) VALUES(6, 'Anne Begge')"
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(1, 1, 'key-1', 1, 2, 10, 20, 0.9, 'test', ?)
                    """,
                    (b"embedding-1",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(2, 1, 'key-2', 3, 4, 12, 22, 0.8, 'test', ?)
                    """,
                    (b"embedding-2",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(3, 1, 'key-3', 5, 6, 14, 24, 0.7, 'test', ?)
                    """,
                    (b"embedding-3",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(4, 1, 'key-4', 7, 8, 16, 26, 0.6, 'test', ?)
                    """,
                    (b"embedding-4",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(5, 1, 'key-5', 9, 10, 18, 28, 0.5, 'test', ?)
                    """,
                    (b"embedding-5",),
                )
                face_conn.execute(
                    "INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)"
                )
                face_conn.execute(
                    "INSERT INTO person_faces(person_id, face_id) VALUES(4, 3)"
                )
                face_conn.execute(
                    "INSERT INTO person_faces(person_id, face_id) VALUES(5, 4)"
                )
                face_conn.execute(
                    "INSERT INTO person_faces(person_id, face_id) VALUES(6, 5)"
                )
                face_conn.execute(
                    """
                    INSERT INTO face_suggestions(person_id, face_id, reference_face_id, similarity)
                    VALUES(2, 2, 1, 0.91)
                    """
                )
                face_conn.execute(
                    """
                    INSERT INTO face_suggestions(person_id, face_id, reference_face_id, similarity)
                    VALUES(3, 2, NULL, 0.81)
                    """
                )
                face_conn.execute(
                    "INSERT INTO person_files(person_id, file_id) VALUES(3, 1)"
                )
                face_conn.execute(
                    "INSERT INTO person_files(person_id, file_id) VALUES(6, 1)"
                )
                face_conn.commit()
            finally:
                face_conn.close()

            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            default_body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
            )
            body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
                person_reference_links_enabled=True,
            )
            manual_disabled_body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
                manual_person_controls_enabled=False,
            )
            face_body = face_overlay_content_html(
                target, item, person_reference_links_enabled=True
            )
            disabled_body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
                face_enabled=False,
                openclip_enabled=False,
            )

        self.assertNotIn('class="person-reference-link"', default_body)
        self.assertNotIn('href="/person/Ola%20Nordmann/confirmed/item/1"', default_body)
        self.assertNotIn("1 filer, 1 måneder", body)
        self.assertIn('class="person-link" href="/person/Kari/no-faces/item/1"', body)
        self.assertIn(
            'data-person-name="Kari" title="Vis alle bilder med denne personen"', body
        )
        self.assertIn(
            'Kari<span class="confirmed-badge" title="Bekreftet" aria-label="Bekreftet"> ✅</span></a>',
            body,
        )
        self.assertIn(
            'class="person-link" href="/person/Ola%20Nordmann/no-faces/item/1"', body
        )
        self.assertIn(
            'data-person-name="Ola Nordmann" title="Vis alle bilder med denne personen">Ola Nordmann</a>',
            body,
        )
        self.assertIn(
            'class="person-link" href="/person/Per%20Manual/no-faces/item/1"', body
        )
        self.assertIn(
            'data-person-name="Per Manual" title="Vis alle bilder med denne personen">'
            'Per Manual<span class="confirmed-badge" title="Bekreftet" aria-label="Bekreftet"> ✅</span></a>',
            body,
        )
        self.assertIn(
            'class="person-link" href="/person/Anne%20Begge/no-faces/item/1"', body
        )
        self.assertIn(
            'data-person-name="Anne Begge" title="Vis alle bilder med denne personen">'
            'Anne Begge<span class="confirmed-badge" title="Bekreftet" aria-label="Bekreftet"> ✅</span></a>',
            body,
        )
        self.assertIn("Bekreft ansikt", body)
        self.assertNotIn("Ubekreftet ansikter i bildet", body)
        tag_rail_start = body.index('<aside class="tag-rail"')
        tag_rail_end = body.index("</aside>", tag_rail_start)
        tag_rail_html = body[tag_rail_start:tag_rail_end]
        self.assertIn("Personer i bildet", tag_rail_html)
        self.assertIn("Referanseansikter:", tag_rail_html)
        self.assertIn(
            'class="person-link" href="/person/Kari/no-faces/item/1"', tag_rail_html
        )
        self.assertIn(
            'class="person-link" href="/person/Ola%20Nordmann/no-faces/item/1"',
            tag_rail_html,
        )
        self.assertIn('class="person-link-with-reference"', tag_rail_html)
        self.assertIn(
            'class="person-reference-link" href="/person/Ola%20Nordmann/confirmed/item/1"',
            tag_rail_html,
        )
        self.assertIn(
            'Ola Nordmann</a><span class="person-reference-marker">(</span>'
            '<a class="person-reference-link" href="/person/Ola%20Nordmann/confirmed/item/1" '
            'title="Vis referansebildet som ga forslaget, face-id 1">r</a>'
            '<span class="person-reference-marker">)</span>',
            tag_rail_html,
        )
        self.assertIn(
            'class="person-link" href="/person/Per%20Manual/no-faces/item/1"',
            tag_rail_html,
        )
        self.assertNotIn('href="/person/Per%20Manual/confirmed/item/', tag_rail_html)
        self.assertIn(
            'class="person-link" href="/person/Anne%20Begge/no-faces/item/1"',
            tag_rail_html,
        )
        self.assertIn(
            'class="person-link" href="/person/Siril/no-faces/item/1"', tag_rail_html
        )
        self.assertIn(
            'class="person-link" href="/person/Viljar/no-faces/item/1"', tag_rail_html
        )
        self.assertIn("Bekreft ansikt", tag_rail_html)
        assumed_people_start = tag_rail_html.index("Personer i bildet")
        faces_button_start = tag_rail_html.index("Bekreft ansikt")
        confirmed_faces_start = tag_rail_html.index("Referanseansikt")
        self.assertNotIn("date-status-badge", tag_rail_html)
        assumed_people_html = tag_rail_html[assumed_people_start:faces_button_start]
        confirmed_faces_html = tag_rail_html[confirmed_faces_start:]
        self.assertIn("Kari", assumed_people_html)
        self.assertIn("Anne Begge", assumed_people_html)
        self.assertIn("Ola Nordmann", assumed_people_html)
        self.assertIn("Per Manual", assumed_people_html)
        self.assertIn("Siril", assumed_people_html)
        self.assertIn("Viljar", assumed_people_html)
        self.assertIn("Anne Begge", confirmed_faces_html)
        self.assertIn("Kari", confirmed_faces_html)
        self.assertIn("Siril", confirmed_faces_html)
        self.assertIn("Viljar", confirmed_faces_html)
        self.assertIn('data-unconfirm-face="1"', confirmed_faces_html)
        self.assertIn('data-unconfirm-face="3"', confirmed_faces_html)
        self.assertIn('data-unconfirm-face="4"', confirmed_faces_html)
        self.assertIn('data-unconfirm-face="5"', confirmed_faces_html)
        self.assertIn('data-unconfirm-person="Anne Begge"', confirmed_faces_html)
        self.assertIn('data-unconfirm-person="Kari"', confirmed_faces_html)
        self.assertIn('data-unconfirm-person="Siril"', confirmed_faces_html)
        self.assertIn('data-unconfirm-person="Viljar"', confirmed_faces_html)
        self.assertEqual(confirmed_faces_html.count(">fjern</button>"), 4)
        self.assertNotIn('class="person-link"', confirmed_faces_html)
        self.assertNotIn("Ola Nordmann", confirmed_faces_html)
        self.assertNotIn("Per Manual", confirmed_faces_html)
        self.assertLess(assumed_people_start, faces_button_start)
        self.assertLess(faces_button_start, confirmed_faces_start)
        self.assertNotIn("Person i bildet", body)
        self.assertIn("Velg person", tag_rail_html)
        self.assertIn("Legg til", tag_rail_html)
        self.assertIn("Ferdig", tag_rail_html)
        self.assertIn("data-open-manual-person-form", tag_rail_html)
        self.assertIn("data-manual-person-form", body)
        self.assertEqual(tag_rail_html.count("data-manual-person-remove"), 2)
        self.assertIn('data-person-name="Per Manual">×</button>', tag_rail_html)
        self.assertIn('data-person-name="Anne Begge">×</button>', tag_rail_html)
        self.assertNotIn('data-person-name="Kari">×</button>', tag_rail_html)
        self.assertNotIn('data-person-name="Siril">×</button>', tag_rail_html)
        self.assertNotIn('data-person-name="Viljar">×</button>', tag_rail_html)
        controls_start = body.index('<nav class="controls"')
        controls_end = body.index("</nav>", controls_start)
        controls_html = body[controls_start:controls_end]
        stage_start = body.index('<section class="stage">')
        stage_end = body.index("</section>", stage_start)
        stage_html = body[stage_start:stage_end]
        self.assertNotIn("data-manual-person-form", controls_html)
        self.assertNotIn("Person i bildet", controls_html)
        self.assertNotIn("data-manual-person-form", stage_html)
        self.assertNotIn("Person i bildet", manual_disabled_body)
        self.assertNotIn("data-manual-person-form", manual_disabled_body)
        self.assertNotIn("data-open-manual-person-form", manual_disabled_body)
        self.assertNotIn("data-manual-person-remove", manual_disabled_body)
        self.assertIn('data-faces-item="1"', body)
        self.assertIn("data-face-list", body)
        self.assertNotIn("Ny person", body)
        self.assertIn("width: fit-content;", SERVER_CSS)
        self.assertIn("justify-self: start;", SERVER_CSS)
        self.assertIn(
            ".tag-rail .person-link {\n      flex: 1 1 max-content;", SERVER_CSS
        )
        self.assertIn(".tag-rail .person-link-with-reference", SERVER_CSS)
        self.assertIn(".person-reference-link", SERVER_CSS)
        self.assertIn(".person-reference-marker", SERVER_CSS)
        self.assertIn("      min-height: 26px;", SERVER_CSS)
        self.assertIn("      background: transparent;", SERVER_CSS)
        self.assertIn(
            ".manual-person-remove-button {\n      display: none;", SERVER_CSS
        )
        self.assertIn(
            ".people-section.manual-person-editing .manual-person-remove-button",
            SERVER_CSS,
        )
        self.assertIn(".inline-link {\n      border: 0;", SERVER_CSS)
        self.assertIn(".tag-rail .faces-button {\n      flex: 0 0 auto;", SERVER_CSS)
        self.assertIn(".controls .delete-button { margin-left: auto; }", SERVER_CSS)
        self.assertIn("Ny person", face_body)
        self.assertIn("/api/face-person-create-and-add-face", SERVER_JS)
        self.assertIn("/api/item-faces?file_id=", SERVER_JS)
        self.assertIn("function ensureRailPersonLink", SERVER_JS)
        self.assertIn(
            "ensureRailPersonLink(payload.person_name, payload.person_url, payload.confirmed);",
            SERVER_JS,
        )
        self.assertIn(
            "ensureRailPersonLink(payload.person_name, payload.person_url, true, true, fileId);",
            SERVER_JS,
        )
        self.assertIn(
            'const addButton = people.querySelector("[data-open-manual-person-form]");',
            SERVER_JS,
        )
        self.assertIn("people.insertBefore(item, addButton);", SERVER_JS)
        self.assertIn('removeButton.dataset.manualPersonRemove = "";', SERVER_JS)
        self.assertIn("wireManualPersonRemoveButton(removeButton);", SERVER_JS)
        self.assertIn('const currentPerson = pathParts[0] === "person"', SERVER_JS)
        self.assertIn("window.location.href = `/item/${fileId}`;", SERVER_JS)
        self.assertIn('section.classList.add("manual-person-editing");', SERVER_JS)
        self.assertIn('section?.classList.remove("manual-person-editing");', SERVER_JS)
        self.assertNotIn("data-remove-person-file", SERVER_JS)
        self.assertNotIn("ensureTopPersonLink", SERVER_JS)
        self.assertIn('document.querySelector(".tag-rail")', SERVER_JS)
        self.assertNotIn('document.querySelector(".topline .people")', SERVER_JS)
        self.assertIn("Identifiser", face_body)
        self.assertIn("Forslag:", face_body)
        self.assertIn("Ola Nordmann <strong>0.910</strong>", face_body)
        self.assertIn('href="/person/Ola%20Nordmann/confirmed/item/1"', face_body)
        self.assertIn('title="Referanse face-id 1">referanse</a>', face_body)
        self.assertIn("Per Manual <strong>0.810</strong>", face_body)
        self.assertNotIn('href="/person/Per%20Manual/confirmed/item/', face_body)
        self.assertIn('data-face-id="2"', face_body)
        self.assertIn('data-person-name="Kari"', face_body)
        self.assertIn('data-person-name="Ola Nordmann"', face_body)
        self.assertNotIn('data-face-id="1"', body)
        self.assertNotIn('data-face-id="1"', face_body)
        self.assertNotIn('href="/people"', disabled_body)
        self.assertNotIn('href="/person/Kari"', disabled_body)
        self.assertNotIn("Personer i bildet", disabled_body)
        self.assertNotIn("Referanseansikt", disabled_body)
        self.assertNotIn('href="/search"', disabled_body)
        self.assertNotIn("Ansikter i bildet", disabled_body)
        self.assertNotIn("Ny person", disabled_body)

    def test_person_reference_links_disabled_uses_base_people_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

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
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.execute("INSERT INTO persons(id, name) VALUES(2, 'Ola')")
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(1, 1, 'key-1', 1, 2, 10, 20, 0.9, 'test', ?)
                    """,
                    (b"embedding-1",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(2, 1, 'key-2', 3, 4, 12, 22, 0.8, 'test', ?)
                    """,
                    (b"embedding-2",),
                )
                face_conn.execute(
                    "INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)"
                )
                face_conn.execute(
                    """
                    INSERT INTO face_suggestions(person_id, face_id, reference_face_id, similarity)
                    VALUES(2, 2, 1, 0.91)
                    """
                )
                face_conn.commit()
            finally:
                face_conn.close()

            real_connect = sqlite3.connect
            queries: list[str] = []

            class RecordingConnection:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    self._conn = real_connect(*args, **kwargs)

                @property
                def row_factory(self) -> object:
                    return self._conn.row_factory

                @row_factory.setter
                def row_factory(self, value: object) -> None:
                    self._conn.row_factory = value

                def execute(self, sql: str, *args: object, **kwargs: object) -> object:
                    queries.append(sql)
                    if "reference_face_id" in sql or "reference_faces" in sql:
                        raise AssertionError(
                            "disabled reference links should not query reference data"
                        )
                    return self._conn.execute(sql, *args, **kwargs)

                def executescript(self, sql: str) -> object:
                    return self._conn.executescript(sql)

                def commit(self) -> None:
                    self._conn.commit()

                def close(self) -> None:
                    self._conn.close()

            clear_face_caches()
            with patch("bildebank.server_faces.sqlite3.connect", RecordingConnection):
                people, confirmed = people_for_file(
                    target, 1, person_reference_links_enabled=False
                )

        self.assertTrue(queries)
        self.assertEqual([person["name"] for person in people], ["Kari", "Ola"])
        self.assertEqual([person["name"] for person in confirmed], ["Kari"])

    def test_run_server_item_faces_api_returns_lazy_overlay_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))

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
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(1, 1, 'key-1', 3, 4, 12, 22, 0.8, 'test', ?)
                    """,
                    (b"embedding-1",),
                )
                face_conn.commit()
            finally:
                face_conn.close()

            response: dict[str, object] = {}
            handler = object.__new__(BildebankRequestHandler)
            handler.server = SimpleNamespace(
                target=target,
                face_enabled=True,
                config=AppConfig(face_recognition=FaceRecognitionConfig(enabled=True)),
            )

            def fake_respond_json(
                content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK
            ) -> None:
                response["content"] = content
                response["status"] = status

            handler.respond_json = fake_respond_json  # type: ignore[method-assign]
            handler.respond_item_faces("file_id=1")

        self.assertEqual(response["status"], HTTPStatus.OK)
        content = response["content"]
        assert isinstance(content, dict)
        self.assertIs(content["ok"], True)
        self.assertIn('data-face-detail="1"', str(content["html"]))
        self.assertIn('data-person-name="Kari"', str(content["html"]))
        self.assertIn("face-box", str(content["html"]))
        self.assertIn("Ingen forslag for dette ansiktet.", str(content["html"]))

    def test_run_server_api_adds_face_to_person(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

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
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(1, 1, 'key-1', 1, 2, 10, 20, 0.9, 'test', ?)
                    """,
                    (b"embedding-1",),
                )
                face_conn.execute(
                    "INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 1, 0.91)"
                )
                face_conn.commit()
            finally:
                face_conn.close()

            data = b"face_id=1&person_name=Kari"

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/x-www-form-urlencoded",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    target=target,
                    config=AppConfig(
                        face_recognition=FaceRecognitionConfig(enabled=True)
                    ),
                )
                body: dict[str, object] | None = None

                def respond_json(
                    self, content: dict[str, object], *, status=None
                ) -> None:
                    self.body = content

            handler = FakeHandler()
            server_endpoints_faces.respond_add_face_to_person(handler)  # type: ignore[arg-type]

            self.assertEqual(
                {
                    "ok": True,
                    "person_name": "Kari",
                    "person_url": "/person/Kari/no-faces/item/1",
                    "confirmed": True,
                    "face_id": 1,
                    "added": True,
                },
                handler.body,
            )
            face_conn = connect_face_db(target)
            try:
                self.assertEqual(
                    face_conn.execute(
                        "SELECT COUNT(*) FROM person_faces WHERE person_id = 1 AND face_id = 1"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    face_conn.execute(
                        "SELECT COUNT(*) FROM face_suggestions"
                    ).fetchone()[0],
                    0,
                )
            finally:
                face_conn.close()

    def test_run_server_api_creates_person_and_adds_face(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

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
            face_conn = connect_face_db(target)
            try:
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(1, 1, 'key-1', 1, 2, 10, 20, 0.9, 'test', ?)
                    """,
                    (b"embedding-1",),
                )
                face_conn.commit()
            finally:
                face_conn.close()

            data = json.dumps({"face_id": 1, "person_name": "Kari"}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    target=target,
                    config=AppConfig(
                        face_recognition=FaceRecognitionConfig(enabled=True)
                    ),
                )
                body: dict[str, object] | None = None

                def respond_json(
                    self, content: dict[str, object], *, status=None
                ) -> None:
                    self.body = content

            handler = FakeHandler()
            server_endpoints_faces.respond_create_person_and_add_face(handler)  # type: ignore[arg-type]

            self.assertEqual(
                {
                    "ok": True,
                    "person_name": "Kari",
                    "person_url": "/person/Kari/no-faces/item/1",
                    "confirmed": True,
                    "face_id": 1,
                    "added": True,
                },
                handler.body,
            )
            face_conn = connect_face_db(target)
            try:
                self.assertEqual(
                    face_conn.execute(
                        "SELECT COUNT(*) FROM persons WHERE name = 'Kari'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    face_conn.execute(
                        "SELECT COUNT(*) FROM person_faces WHERE face_id = 1"
                    ).fetchone()[0],
                    1,
                )
            finally:
                face_conn.close()

    def test_run_server_api_face_write_reports_target_lock_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / LOCK_FILENAME).write_text("command=face-scan\n", encoding="utf-8")
            data = json.dumps({"face_id": 1, "person_name": "Kari"}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    target=target,
                    config=AppConfig(
                        face_recognition=FaceRecognitionConfig(enabled=True)
                    ),
                )
                body: dict[str, object] | None = None
                status: HTTPStatus | None = None

                def respond_json(
                    self,
                    content: dict[str, object],
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    self.body = content
                    self.status = status

            handler = FakeHandler()
            server_endpoints_faces.respond_create_person_and_add_face(handler)  # type: ignore[arg-type]

        self.assertEqual(handler.status, HTTPStatus.CONFLICT)
        self.assertIn("Bildesamlingen er låst", str(handler.body["error"]))

    def test_run_server_api_removes_face_from_person(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

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
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(1, 1, 'key-1', 1, 2, 10, 20, 0.9, 'test', ?)
                    """,
                    (b"embedding-1",),
                )
                face_conn.execute(
                    "INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)"
                )
                face_conn.commit()
            finally:
                face_conn.close()

            data = json.dumps({"face_id": 1, "person_name": "Kari"}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    target=target,
                    config=AppConfig(
                        face_recognition=FaceRecognitionConfig(enabled=True)
                    ),
                )
                body: dict[str, object] | None = None

                def respond_json(
                    self, content: dict[str, object], *, status=None
                ) -> None:
                    self.body = content

            handler = FakeHandler()
            server_endpoints_faces.respond_remove_face_from_person(handler)  # type: ignore[arg-type]

            self.assertEqual(
                {
                    "ok": True,
                    "person_name": "Kari",
                    "person_url": "/person/Kari",
                    "face_id": 1,
                    "file_id": 1,
                    "redirect_url": "/item/1",
                    "removed": True,
                },
                handler.body,
            )
            face_conn = connect_face_db(target)
            try:
                self.assertEqual(
                    face_conn.execute("SELECT COUNT(*) FROM person_faces").fetchone()[
                        0
                    ],
                    0,
                )
            finally:
                face_conn.close()

    def test_run_server_api_adds_and_removes_manual_person_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

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
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.commit()
            finally:
                face_conn.close()

            data = json.dumps({"file_id": 1, "person_name": "Kari"}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    target=target,
                    config=AppConfig(
                        face_recognition=FaceRecognitionConfig(enabled=True)
                    ),
                )
                body: dict[str, object] | None = None
                status = None

                def respond_json(
                    self, content: dict[str, object], *, status=None
                ) -> None:
                    self.body = content
                    self.status = status

            handler = FakeHandler()
            server_endpoints_faces.respond_add_person_to_file(handler)  # type: ignore[arg-type]

            self.assertIsNone(handler.status)
            self.assertEqual(
                {
                    "ok": True,
                    "person_name": "Kari",
                    "person_url": "/person/Kari/no-faces/item/1",
                    "confirmed": True,
                    "file_id": 1,
                    "added": True,
                },
                handler.body,
            )
            face_conn = connect_face_db(target)
            try:
                self.assertEqual(
                    face_conn.execute("SELECT COUNT(*) FROM person_files").fetchone()[
                        0
                    ],
                    1,
                )
            finally:
                face_conn.close()

            handler = FakeHandler()
            handler.rfile = BytesIO(data)
            server_endpoints_faces.respond_remove_person_from_file(handler)  # type: ignore[arg-type]

            self.assertIsNone(handler.status)
            self.assertEqual(
                {
                    "ok": True,
                    "person_name": "Kari",
                    "person_url": "/person/Kari",
                    "file_id": 1,
                    "removed": True,
                },
                handler.body,
            )
            face_conn = connect_face_db(target)
            try:
                self.assertEqual(
                    face_conn.execute("SELECT COUNT(*) FROM person_files").fetchone()[
                        0
                    ],
                    0,
                )
            finally:
                face_conn.close()

    def test_manual_person_file_requires_existing_person_and_active_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")

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
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.commit()
            finally:
                face_conn.close()

            with self.assertRaisesRegex(ValueError, "Fant ikke person: Ola"):
                add_person_to_file(target, "Ola", 1)
            with self.assertRaisesRegex(ValueError, "Fant ikke aktiv fil-id 999"):
                add_person_to_file(target, "Kari", 999)

            result = add_person_to_file(target, "Kari", 1)
            self.assertTrue(result.added)
            removed = remove_person_from_file(target, "Kari", 1)
            self.assertTrue(removed.removed)

    def test_run_server_api_manual_person_file_is_disabled_when_faces_are_disabled(
        self,
    ) -> None:
        class FakeHandler:
            path = "/api/face-person-add-file"
            headers = {"X-CSRF-Token": "test-token"}
            rfile = BytesIO()
            server = SimpleNamespace(face_enabled=False, csrf_token="test-token")
            body: dict[str, object] | None = None
            status = None

            def respond_json(self, content: dict[str, object], *, status=None) -> None:
                self.body = content
                self.status = status

        handler = FakeHandler()
        BildebankRequestHandler.do_POST(handler)  # type: ignore[arg-type]

        self.assertEqual(HTTPStatus.FORBIDDEN, handler.status)
        self.assertEqual(
            {"ok": False, "error": "Ansiktsgjenkjenning er av."}, handler.body
        )

    def test_run_server_api_renames_person(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.commit()
            finally:
                face_conn.close()

            data = json.dumps({"old_name": "Kari", "new_name": "Kari Nordmann"}).encode(
                "utf-8"
            )

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    target=target,
                    config=AppConfig(
                        face_recognition=FaceRecognitionConfig(enabled=True)
                    ),
                )
                body: dict[str, object] | None = None

                def respond_json(
                    self, content: dict[str, object], *, status=None
                ) -> None:
                    self.body = content

            handler = FakeHandler()
            server_endpoints_faces.respond_rename_person(handler)  # type: ignore[arg-type]

            self.assertEqual(
                {
                    "ok": True,
                    "old_name": "Kari",
                    "new_name": "Kari Nordmann",
                    "person_url": "/person/Kari%20Nordmann/no-faces",
                },
                handler.body,
            )
            face_conn = connect_face_db(target)
            try:
                self.assertEqual(
                    face_conn.execute(
                        "SELECT COUNT(*) FROM persons WHERE name = 'Kari'"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    face_conn.execute(
                        "SELECT COUNT(*) FROM persons WHERE name = 'Kari Nordmann'"
                    ).fetchone()[0],
                    1,
                )
            finally:
                face_conn.close()

    def test_run_server_api_rename_person_reports_existing_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.execute("INSERT INTO persons(id, name) VALUES(2, 'Ola')")
                face_conn.commit()
            finally:
                face_conn.close()

            data = json.dumps({"old_name": "Kari", "new_name": "Ola"}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    target=target,
                    config=AppConfig(
                        face_recognition=FaceRecognitionConfig(enabled=True)
                    ),
                )
                body: dict[str, object] | None = None
                status = None

                def respond_json(
                    self, content: dict[str, object], *, status=None
                ) -> None:
                    self.body = content
                    self.status = status

            handler = FakeHandler()
            server_endpoints_faces.respond_rename_person(handler)  # type: ignore[arg-type]

            self.assertEqual(HTTPStatus.BAD_REQUEST, handler.status)
            self.assertEqual(
                {"ok": False, "error": "Person finnes allerede: Ola"}, handler.body
            )

    def test_run_server_api_rename_person_validates_name_and_accepts_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.commit()
            finally:
                face_conn.close()

            def call_rename(payload: dict[str, object]):
                data = json.dumps(payload).encode("utf-8")

                class FakeHandler:
                    headers = {
                        "Content-Length": str(len(data)),
                        "Content-Type": "application/json",
                    }
                    rfile = BytesIO(data)
                    server = SimpleNamespace(
                        target=target,
                        config=AppConfig(
                            face_recognition=FaceRecognitionConfig(enabled=True)
                        ),
                    )
                    body: dict[str, object] | None = None
                    status = None

                    def respond_json(
                        self, content: dict[str, object], *, status=None
                    ) -> None:
                        self.body = content
                        self.status = status

                handler = FakeHandler()
                server_endpoints_faces.respond_rename_person(handler)  # type: ignore[arg-type]
                return handler

            missing_name = call_rename({"old_name": "Kari", "new_name": " "})
            same_name = call_rename({"old_name": " Kari ", "new_name": "Kari"})

            self.assertEqual(HTTPStatus.BAD_REQUEST, missing_name.status)
            self.assertEqual(
                {"ok": False, "error": "Nytt personnavn mangler."}, missing_name.body
            )
            self.assertEqual(
                {
                    "ok": True,
                    "old_name": "Kari",
                    "new_name": "Kari",
                    "person_url": "/person/Kari/no-faces",
                },
                same_name.body,
            )

    def test_run_server_api_rename_person_is_disabled_when_faces_are_disabled(
        self,
    ) -> None:
        class FakeHandler:
            path = "/api/face-person-rename"
            headers = {"X-CSRF-Token": "test-token"}
            rfile = BytesIO()
            server = SimpleNamespace(face_enabled=False, csrf_token="test-token")
            body: dict[str, object] | None = None
            status = None

            def respond_json(self, content: dict[str, object], *, status=None) -> None:
                self.body = content
                self.status = status

        handler = FakeHandler()
        BildebankRequestHandler.do_POST(handler)  # type: ignore[arg-type]

        self.assertEqual(HTTPStatus.FORBIDDEN, handler.status)
        self.assertEqual(
            {"ok": False, "error": "Ansiktsgjenkjenning er av."}, handler.body
        )

    def test_run_server_api_delete_person_removes_person_links_and_suggestions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"

            self.assertEqual(run_cli(["create", str(target)]), 0)
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(1, 1, 'key-1', 1, 2, 10, 20, 0.9, 'test', ?)
                    """,
                    (b"embedding-1",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(2, 2, 'key-2', 3, 4, 12, 22, 0.8, 'test', ?)
                    """,
                    (b"embedding-2",),
                )
                face_conn.execute(
                    "INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)"
                )
                face_conn.execute(
                    "INSERT INTO person_files(person_id, file_id) VALUES(1, 1)"
                )
                face_conn.execute(
                    "INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 2, 0.91)"
                )
                face_conn.commit()
            finally:
                face_conn.close()

            data = json.dumps({"person_name": "Kari"}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    target=target,
                    config=AppConfig(
                        face_recognition=FaceRecognitionConfig(enabled=True)
                    ),
                )
                body: dict[str, object] | None = None
                status = None

                def respond_json(
                    self, content: dict[str, object], *, status=None
                ) -> None:
                    self.body = content
                    self.status = status

            handler = FakeHandler()
            server_endpoints_faces.respond_delete_person(handler)  # type: ignore[arg-type]

            face_conn = connect_face_db(target)
            try:
                person_count = face_conn.execute(
                    "SELECT COUNT(*) FROM persons"
                ).fetchone()[0]
                link_count = face_conn.execute(
                    "SELECT COUNT(*) FROM person_faces"
                ).fetchone()[0]
                manual_link_count = face_conn.execute(
                    "SELECT COUNT(*) FROM person_files"
                ).fetchone()[0]
                suggestion_count = face_conn.execute(
                    "SELECT COUNT(*) FROM face_suggestions"
                ).fetchone()[0]
                face_count = face_conn.execute("SELECT COUNT(*) FROM faces").fetchone()[
                    0
                ]
            finally:
                face_conn.close()

        self.assertIsNone(handler.status)
        self.assertEqual(
            {
                "ok": True,
                "person_name": "Kari",
                "removed_faces": 1,
                "removed_files": 1,
                "removed_suggestions": 1,
            },
            handler.body,
        )
        self.assertEqual(0, person_count)
        self.assertEqual(0, link_count)
        self.assertEqual(0, manual_link_count)
        self.assertEqual(0, suggestion_count)
        self.assertEqual(2, face_count)

    def test_run_server_api_delete_person_is_disabled_when_faces_are_disabled(
        self,
    ) -> None:
        class FakeHandler:
            path = "/api/face-person-delete"
            headers = {"X-CSRF-Token": "test-token"}
            rfile = BytesIO()
            server = SimpleNamespace(face_enabled=False, csrf_token="test-token")
            body: dict[str, object] | None = None
            status = None

            def respond_json(self, content: dict[str, object], *, status=None) -> None:
                self.body = content
                self.status = status

        handler = FakeHandler()
        BildebankRequestHandler.do_POST(handler)  # type: ignore[arg-type]

        self.assertEqual(HTTPStatus.FORBIDDEN, handler.status)
        self.assertEqual(
            {"ok": False, "error": "Ansiktsgjenkjenning er av."}, handler.body
        )

    def test_run_server_person_browser_filters_and_marks_faces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))
            (source / "IMG_20240203.png").write_bytes(minimal_png(101, 80))
            (source / "IMG_20250104.png").write_bytes(minimal_png(102, 80))

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
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.execute("INSERT INTO persons(id, name) VALUES(2, 'Ola')")
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(1, 1, 'key-1', 1, 2, 10, 20, 0.9, 'test', ?)
                    """,
                    (b"embedding-1",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(2, 2, 'key-2', 3, 4, 12, 22, 0.8, 'test', ?)
                    """,
                    (b"embedding-2",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(3, 3, 'key-3', 5, 6, 14, 24, 0.7, 'test', ?)
                    """,
                    (b"embedding-3",),
                )
                face_conn.execute(
                    "INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)"
                )
                face_conn.execute(
                    "INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 2, 0.91)"
                )
                face_conn.execute(
                    "INSERT INTO person_faces(person_id, face_id) VALUES(2, 3)"
                )
                face_conn.execute(
                    "INSERT INTO person_files(person_id, file_id) VALUES(1, 1)"
                )
                face_conn.commit()
            finally:
                face_conn.close()
            conn = db.connect(target)
            try:
                db.rotate_file_view(conn, 1, "right")
                conn.commit()
            finally:
                conn.close()
            cached_image_dimensions(target, target / "2024" / "01" / "IMG_20240102.png")
            cached_image_orientation(
                target, target / "2024" / "01" / "IMG_20240102.png"
            )

            item = person_item_by_id(target, "Kari", 1)
            self.assertIsNotNone(item)
            with (
                patch(
                    "bildebank.server_faces.image_dimensions",
                    side_effect=AssertionError("cached dimensions should be used"),
                ),
                patch(
                    "bildebank.server_faces.image_orientation",
                    side_effect=AssertionError("cached orientation should be used"),
                ),
            ):
                body = person_item_page_html(
                    target,
                    "Kari",
                    item,
                    *adjacent_person_items(target, "Kari", item),
                    person_month_navigation(target, "Kari", item),
                )
            plain_source = person_browser_source(
                "Kari", include_suggestions=True, show_faces=False
            )
            plain_item = source_item_by_id(target, plain_source, 1)
            self.assertIsNotNone(plain_item)
            plain_body = source_item_page_html(
                target,
                plain_source,
                plain_item,
                *adjacent_source_items(target, plain_source, plain_item),
                source_month_navigation(target, plain_source, plain_item),
            )
            suggested_item = source_item_by_id(target, plain_source, 2)
            self.assertIsNotNone(suggested_item)
            suggested_body = source_item_page_html(
                target,
                plain_source,
                suggested_item,
                *adjacent_source_items(target, plain_source, suggested_item),
                source_month_navigation(target, plain_source, suggested_item),
            )
            plain_years_body = source_years_page_html(
                target, plain_source, face_config=FaceRecognitionConfig(enabled=True)
            )
            confirmed_plain_source = person_browser_source(
                "Kari", include_suggestions=False, show_faces=False
            )
            confirmed_years_body = source_years_page_html(
                target,
                confirmed_plain_source,
                face_config=FaceRecognitionConfig(enabled=True),
            )
            marked_source = person_browser_source(
                "Kari", include_suggestions=True, show_faces=True
            )
            marked_years_body = source_years_page_html(
                target, marked_source, face_config=FaceRecognitionConfig(enabled=True)
            )
            month_body = person_month_page_html(
                target, "Kari", "2024-02", person_month_items(target, "Kari", "2024-02")
            )

        self.assertIn(">Kari<", body)
        self.assertIn("/person/Kari/month/2024-02", body)
        self.assertIn('href="/item/1">Åpne i alle bilder</a>', body)
        controls_start = body.index('<nav class="controls"')
        controls_end = body.index("</nav>", controls_start)
        controls_html = body[controls_start:controls_end]
        self.assertIn(
            '<a class="nav-button" href="/person/Kari/no-faces/item/1" title="Skjul ansiktsmarkering"><span class="face-toggle-icon face-toggle-icon-active">👤</span></a>',
            controls_html,
        )
        self.assertIn(".face-toggle-icon-active", SERVER_CSS)
        self.assertIn(
            '<a class="nav-button" href="/person/Kari/confirmed/item/1">[✓] Ta med forslag</a>',
            controls_html,
        )
        self.assertNotIn("Bare bekreftede", body)
        self.assertNotIn("Med forslag", body)
        self.assertNotIn("Uten ansiktsmarkering", body)
        self.assertNotIn("Med ansiktsmarkering", body)
        self.assertIn("person-face-box", body)
        self.assertIn("person-face-layer", body)
        self.assertIn("bekreftet face-id 1", body)
        self.assertIn('<span class="person-face-label">face-id 1</span>', body)
        self.assertIn(
            '<div class="person-media" style="transform: rotate(90deg); --quarter-turn-width:',
            body,
        )
        self.assertIn('<a href="/file/1" target="_blank"><img src="/file/1"', body)
        self.assertIn(".person-face-layer", SERVER_CSS)
        self.assertIn("function fitPersonFaceLayer", SERVER_JS)
        self.assertIn("function observePersonFaceLayers", SERVER_JS)
        self.assertIn("ResizeObserver", SERVER_JS)
        self.assertIn('data-view-rotation="90">', body)
        self.assertNotIn("IMG_20250104", body)
        self.assertIn("Kari - uten ansiktsmarkering", plain_body)
        self.assertIn('href="/person/Kari/no-faces">Kari</a>', plain_body)
        self.assertIn('href="/person/Kari/no-faces/year/2024">2024</a>', plain_body)
        self.assertIn("Kari - uten ansiktsmarkering", plain_years_body)
        self.assertIn('href="/person/Kari/no-faces/year/2024"', plain_years_body)
        self.assertIn(">2024</div>", plain_years_body)
        self.assertIn(">2 måneder, 2 bilder</div>", plain_years_body)
        self.assertIn(
            'href="/person/Kari/no-faces/month/2024-01" title="Neste måned" data-key-nav="next-month">ed ▶</a>',
            plain_years_body,
        )
        self.assertIn("Kari - bekreftet - uten ansiktsmarkering", confirmed_years_body)
        self.assertIn(
            'href="/person/Kari/confirmed/no-faces/year/2024"', confirmed_years_body
        )
        self.assertIn(">1 måned, 1 bilde</div>", confirmed_years_body)
        self.assertNotIn(
            "/person/Kari/confirmed/no-faces/year/2025", confirmed_years_body
        )
        self.assertIn('href="/person/Kari/year/2024"', marked_years_body)
        self.assertNotIn(
            'Kari - uten ansiktsmarkering</a><span class="sep">/</span>', plain_body
        )
        plain_tag_rail_start = plain_body.index('<aside class="tag-rail"')
        plain_tag_rail_end = plain_body.index("</aside>", plain_tag_rail_start)
        plain_tag_rail_html = plain_body[plain_tag_rail_start:plain_tag_rail_end]
        self.assertIn("Personer i bildet", plain_tag_rail_html)
        self.assertIn("data-open-manual-person-form", plain_tag_rail_html)
        self.assertIn("data-manual-person-form", plain_tag_rail_html)
        self.assertIn("Velg person", plain_tag_rail_html)
        self.assertIn("Legg til", plain_tag_rail_html)
        self.assertIn("Ferdig", plain_tag_rail_html)
        self.assertIn(
            'data-manual-person-remove data-file-id="1" data-person-name="Kari">×</button>',
            plain_tag_rail_html,
        )
        plain_controls_start = plain_body.index('<nav class="controls"')
        plain_controls_end = plain_body.index("</nav>", plain_controls_start)
        plain_controls_html = plain_body[plain_controls_start:plain_controls_end]
        self.assertNotIn("Fjern manuell person-i-bilde", plain_controls_html)
        self.assertNotIn("data-remove-person-file", plain_controls_html)
        self.assertIn(
            '<a class="nav-button" href="/person/Kari/item/1" title="Vis ansiktsmarkering"><span class="face-toggle-icon">👤</span></a>',
            plain_controls_html,
        )
        self.assertNotIn("face-toggle-icon-active", plain_controls_html)
        self.assertIn(
            '<a class="nav-button" href="/person/Kari/confirmed/no-faces/item/1">[✓] Ta med forslag</a>',
            plain_controls_html,
        )
        self.assertIn('href="/item/1">Åpne i alle bilder</a>', plain_body)
        self.assertNotIn("Med ansiktsmarkering", plain_body)
        self.assertNotIn('<div class="person-face-box"', plain_body)
        self.assertNotIn('<span class="person-face-label">face-id 1</span>', plain_body)
        self.assertIn('<img src="/display/1"', plain_body)
        suggested_controls_start = suggested_body.index('<nav class="controls"')
        suggested_controls_end = suggested_body.index(
            "</nav>", suggested_controls_start
        )
        suggested_controls_html = suggested_body[
            suggested_controls_start:suggested_controls_end
        ]
        self.assertIn(
            '<a class="nav-button" href="/person/Kari/confirmed/no-faces">[✓] Ta med forslag</a>',
            suggested_controls_html,
        )
        self.assertNotIn(
            '/person/Kari/confirmed/no-faces/item/2">[✓] Ta med forslag</a>',
            suggested_controls_html,
        )
        month_controls_start = month_body.index('<nav class="controls"')
        month_controls_end = month_body.index("</nav>", month_controls_start)
        month_controls_html = month_body[month_controls_start:month_controls_end]
        self.assertIn(
            '<a class="nav-button" href="/person/Kari/confirmed">[✓] Ta med forslag</a>',
            month_controls_html,
        )
        self.assertIn("/person/Kari/item/2", month_body)
        self.assertNotIn("/person/Kari/item/3", month_body)

    def test_run_server_person_browser_uses_sql_filter_for_item_navigation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))
            (source / "IMG_20240203.png").write_bytes(minimal_png(101, 80))
            (source / "IMG_20240304.png").write_bytes(minimal_png(102, 80))

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
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(1, 1, 'key-1', 1, 2, 10, 20, 0.9, 'test', ?)
                    """,
                    (b"embedding-1",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(2, 2, 'key-2', 3, 4, 12, 22, 0.8, 'test', ?)
                    """,
                    (b"embedding-2",),
                )
                face_conn.execute(
                    "INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)"
                )
                face_conn.execute(
                    "INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 2, 0.91)"
                )
                face_conn.execute(
                    "INSERT INTO person_files(person_id, file_id) VALUES(1, 3)"
                )
                face_conn.commit()
            finally:
                face_conn.close()

            all_source = person_browser_source(
                "Kari", include_suggestions=True, show_faces=False
            )
            confirmed_source = person_browser_source(
                "Kari", include_suggestions=False, show_faces=False
            )

            with patch(
                "bildebank.server_browser_queries.source_items",
                side_effect=AssertionError("person browser should use SQL filter"),
            ):
                all_item = source_item_by_id(target, all_source, 2)
                self.assertIsNotNone(all_item)
                previous_item, next_item = adjacent_source_items(
                    target, all_source, all_item
                )
                all_month_nav = source_month_navigation(target, all_source, all_item)
                manual_month_items = source_month_items(target, all_source, "2024-03")
                confirmed_item = source_item_by_id(target, confirmed_source, 1)
                self.assertIsNotNone(confirmed_item)

                self.assertTrue(source_has_sql_filter(all_source))
                self.assertTrue(source_has_sql_filter(confirmed_source))
                self.assertEqual(3, source_item_count(target, all_source))
                self.assertEqual(1, source_item_count(target, confirmed_source))
                self.assertEqual(1, int(previous_item["id"]))
                self.assertEqual(3, int(next_item["id"]))
                self.assertEqual(
                    {
                        "previous_year": None,
                        "next_year": None,
                        "previous_month": "2024-01",
                        "next_month": "2024-03",
                    },
                    all_month_nav,
                )
                self.assertEqual([3], [int(item["id"]) for item in manual_month_items])
                self.assertIsNone(source_item_by_id(target, confirmed_source, 2))
                self.assertIsNone(source_item_by_id(target, confirmed_source, 3))

                class FakeServer:
                    def __init__(self, target: Path) -> None:
                        self.target = target
                        self.config = AppConfig(
                            face_recognition=FaceRecognitionConfig(enabled=True),
                            openclip=OpenClipConfig(enabled=False),
                        )
                        self.face_enabled = True
                        self.openclip_enabled = False
                        self.hide_out_of_focus = False
                        self._source_item_ids = {}
                        self._source_first_day_item_ids = {}
                        self._browser_navigation_cache_version = 0

                    def source_month_keys(
                        self, source, *, hide_out_of_focus: bool = False
                    ):
                        return source_month_keys(
                            self.target,
                            source,
                            self.config.face_recognition,
                            hide_out_of_focus=hide_out_of_focus,
                        )

                    def source_item_order(
                        self, source, *, hide_out_of_focus: bool = False
                    ):
                        return BildebankServer.source_item_order(
                            self,
                            source,
                            hide_out_of_focus=hide_out_of_focus,
                        )

                    def browser_navigation_cache_version(self) -> int:
                        return 0

                    def source_first_day_item_id(
                        self, source, day_key: str, *, hide_out_of_focus: bool = False
                    ):
                        return BildebankServer.source_first_day_item_id(
                            self,
                            source,
                            day_key,
                            hide_out_of_focus=hide_out_of_focus,
                        )

                class FakeHandler:
                    server = FakeServer(target)
                    body = ""
                    status = None

                    def respond_html(self, body: str, *, status=HTTPStatus.OK) -> None:
                        self.body = body
                        self.status = status

                    def respond_text(self, body: str, *, status=HTTPStatus.OK) -> None:
                        self.body = body
                        self.status = status

                    def redirect(self, location: str) -> None:
                        self.body = location
                        self.status = HTTPStatus.FOUND

                    def record_server_timing(self, name: str, start: float) -> None:
                        return

                handler = FakeHandler()
                with (
                    patch(
                        "bildebank.server_runtime.source_item_ids",
                        wraps=source_item_ids,
                    ) as item_ids_mock,
                    patch(
                        "bildebank.server_endpoints_browser.source_item_by_id",
                        wraps=source_item_by_id,
                    ) as item_by_id_mock,
                    patch(
                        "bildebank.server_endpoints_browser.adjacent_source_items",
                        wraps=adjacent_source_items,
                    ) as adjacent_mock,
                    patch(
                        "bildebank.server_browser_queries.first_source_day_item",
                        side_effect=AssertionError(
                            "handler should pass cached first day item"
                        ),
                    ),
                ):
                    server_endpoints_browser.respond_browser_source(  # type: ignore[arg-type]
                        handler,
                        all_source,
                        "item",
                        "2",
                        face_config=handler.server.config.face_recognition,
                        item_not_found_message="Filen finnes ikke for denne personen.",
                        invalid_page_message="Ugyldig personside.",
                    )

                self.assertEqual(HTTPStatus.OK, handler.status)
                self.assertIn('data-browser-item-id="2"', handler.body)
                self.assertEqual(1, item_ids_mock.call_count)
                self.assertEqual(0, item_by_id_mock.call_count)
                self.assertEqual(0, adjacent_mock.call_count)

    def test_run_server_people_page_links_confirmed_and_suggested_person_browser(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))
            (source / "IMG_20240203.png").write_bytes(minimal_png(101, 80))

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
            main_conn = db.connect(target)
            try:
                file_rows = {
                    int(row["id"]): row
                    for row in main_conn.execute(
                        "SELECT id, target_path, target_path_key, sha256 FROM files"
                    )
                }
            finally:
                main_conn.close()
            face_conn = connect_face_db(target)
            try:
                face_conn.execute(
                    """
                    INSERT INTO scanned_files(file_id, target_path, target_path_key, sha256, status, face_count)
                    VALUES(1, ?, ?, ?, 'ok', 2)
                    """,
                    (
                        file_rows[1]["target_path"],
                        file_rows[1]["target_path_key"],
                        file_rows[1]["sha256"],
                    ),
                )
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(1, 1, 'key-1', 1, 2, 10, 20, 0.9, 'test', ?)
                    """,
                    (b"embedding-1",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(2, 2, 'key-2', 3, 4, 12, 22, 0.8, 'test', ?)
                    """,
                    (b"embedding-2",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(3, 1, 'key-3', 5, 6, 14, 24, 0.7, 'test', ?)
                    """,
                    (b"embedding-3",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(4, 999, 'stale-confirmed', 5, 6, 14, 24, 0.7, 'test', ?)
                    """,
                    (b"embedding-4",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(5, 1000, 'stale-suggested', 5, 6, 14, 24, 0.7, 'test', ?)
                    """,
                    (b"embedding-5",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(6, 2, 'key-2-extra', 7, 8, 16, 26, 0.7, 'test', ?)
                    """,
                    (b"embedding-6",),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(7, 1, 'key-1-suggested', 7, 8, 16, 26, 0.7, 'test', ?)
                    """,
                    (b"embedding-7",),
                )
                face_conn.execute(
                    "INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)"
                )
                face_conn.execute(
                    "INSERT INTO person_faces(person_id, face_id) VALUES(1, 3)"
                )
                face_conn.execute(
                    "INSERT INTO person_faces(person_id, face_id) VALUES(1, 4)"
                )
                face_conn.execute(
                    "INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 2, 0.91)"
                )
                face_conn.execute(
                    "INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 5, 0.92)"
                )
                face_conn.execute(
                    "INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 6, 0.90)"
                )
                face_conn.execute(
                    "INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(1, 7, 0.89)"
                )
                face_conn.commit()
            finally:
                face_conn.close()

            cached_person_file_ids.cache_clear()
            first_file_ids = person_file_ids(target, "Kari", include_suggestions=True)
            cache_after_first = cached_person_file_ids.cache_info()
            second_file_ids = person_file_ids(target, "Kari", include_suggestions=True)
            cache_after_second = cached_person_file_ids.cache_info()
            body = people_page_html(target)
            confirmed_items = person_items(target, "Kari", include_suggestions=False)
            all_items = person_items(target, "Kari", include_suggestions=True)
            confirmed_source = person_browser_source("Kari", include_suggestions=False)
            confirmed_item = source_item_by_id(target, confirmed_source, 1)
            self.assertIsNotNone(confirmed_item)
            with patch(
                "bildebank.server_browser_queries.source_item_by_id",
                wraps=source_item_by_id,
            ) as item_by_id_mock:
                confirmed_body = source_item_page_html(
                    target,
                    confirmed_source,
                    confirmed_item,
                    *adjacent_source_items(target, confirmed_source, confirmed_item),
                    source_month_navigation(target, confirmed_source, confirmed_item),
                )
            self.assertEqual(item_by_id_mock.call_count, 0)
            confirmed_month_body = source_month_page_html(
                target,
                confirmed_source,
                "2024-01",
                source_month_items(target, confirmed_source, "2024-01"),
            )

        self.assertEqual(first_file_ids, [1, 2])
        self.assertEqual(second_file_ids, [1, 2])
        self.assertEqual(cache_after_first.misses, 1)
        self.assertEqual(cache_after_second.hits, cache_after_first.hits + 1)
        self.assertIn(
            "<div><strong>Antall bilder i databasen</strong><span>2</span></div>", body
        )
        self.assertIn(
            "<div><strong>Scannet av face-scan</strong><span>1</span></div>", body
        )
        self.assertIn(
            "<div><strong>Ikke scannet av face-scan</strong><span>1</span></div>", body
        )
        self.assertIn("<div><strong>Ansikter funnet</strong><span>5</span></div>", body)
        self.assertIn(
            "<div><strong>Ansikter med forslag</strong><span>3</span></div>", body
        )
        self.assertNotIn('href="/person/Kari/confirmed/no-faces"', body)
        self.assertIn('href="/person/Kari/no-faces"', body)
        self.assertIn("data-open-person-rename", body)
        self.assertIn('data-person-name="Kari"', body)
        self.assertIn("endre navn", body)
        self.assertIn('data-delete-person-name="Kari"', body)
        self.assertIn("slett person", body)
        self.assertIn('id="personRenameDialog"', body)
        self.assertNotIn("Bekreftede bilder (1)", body)
        self.assertIn("Bekreftede og forslag (2)", body)
        self.assertNotIn("forslag:", body)
        self.assertIn("NB: 2 bekreftede ansikter i samme bilde", body)
        self.assertIn(
            "NB: 2 bekreftede ansikter for Kari i dette bildet", confirmed_body
        )
        confirmed_controls_start = confirmed_body.index('<nav class="controls"')
        confirmed_controls_end = confirmed_body.index(
            "</nav>", confirmed_controls_start
        )
        confirmed_controls_html = confirmed_body[
            confirmed_controls_start:confirmed_controls_end
        ]
        self.assertIn(
            '<a class="nav-button" href="/person/Kari/item/1">[&nbsp;&nbsp;&nbsp;] Ta med forslag</a>',
            confirmed_controls_html,
        )
        self.assertNotIn("Bare bekreftede", confirmed_body)
        self.assertNotIn("Med forslag", confirmed_body)
        confirmed_month_controls_start = confirmed_month_body.index(
            '<nav class="controls"'
        )
        confirmed_month_controls_end = confirmed_month_body.index(
            "</nav>", confirmed_month_controls_start
        )
        confirmed_month_controls_html = confirmed_month_body[
            confirmed_month_controls_start:confirmed_month_controls_end
        ]
        self.assertIn(
            '<a class="nav-button" href="/person/Kari">[&nbsp;&nbsp;&nbsp;] Ta med forslag</a>',
            confirmed_month_controls_html,
        )
        confirmed_tag_rail_start = confirmed_body.index('<aside class="tag-rail"')
        confirmed_tag_rail_end = confirmed_body.index(
            "</aside>", confirmed_tag_rail_start
        )
        confirmed_tag_rail_html = confirmed_body[
            confirmed_tag_rail_start:confirmed_tag_rail_end
        ]
        self.assertIn('class="person-link"', confirmed_tag_rail_html)
        self.assertIn('data-person-name="Kari"', confirmed_tag_rail_html)
        self.assertIn('data-unconfirm-face="1"', confirmed_tag_rail_html)
        self.assertIn('data-unconfirm-face="3"', confirmed_tag_rail_html)
        self.assertIn('data-unconfirm-person="Kari"', confirmed_tag_rail_html)
        self.assertIn(">fjern</button>", confirmed_tag_rail_html)
        self.assertIn('title="Avkreft face-id 1"', confirmed_tag_rail_html)
        confirmed_controls_start = confirmed_body.index('<nav class="controls"')
        confirmed_controls_end = confirmed_body.index(
            "</nav>", confirmed_controls_start
        )
        confirmed_controls_html = confirmed_body[
            confirmed_controls_start:confirmed_controls_end
        ]
        self.assertNotIn("Avbekreft face-id", confirmed_controls_html)
        self.assertNotIn("data-unconfirm-face", confirmed_controls_html)
        self.assertIn("/api/face-person-remove-face", SERVER_JS)
        self.assertIn("/api/face-person-rename", SERVER_JS)
        self.assertIn("/api/face-person-delete", SERVER_JS)
        self.assertEqual([int(item["id"]) for item in confirmed_items], [1])
        self.assertEqual([int(item["id"]) for item in all_items], [1, 2])
