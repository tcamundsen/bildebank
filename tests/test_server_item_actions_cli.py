from __future__ import annotations

import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from bildebank import db, server_endpoints_items
from bildebank.config import (
    AppConfig,
    BrowserConfig,
    BrowserHotkeyConfig,
    FaceRecognitionConfig,
)
from bildebank.db import DB_FILENAME
from bildebank.face import connect_face_db, create_person
from bildebank.geo import h3_cells_for_point
from bildebank.server_actions import undelete_file_from_browser
from bildebank.server_assets import SERVER_CSS, SERVER_JS
from bildebank.server_browser_info_html import image_info_content_html
from bildebank.server_browser_queries import (
    adjacent_browser_items,
    browser_item_by_id,
    browser_month_items,
    browser_month_navigation,
)
from bildebank.server_filter import text_filter_browser_source
from bildebank.server_pages import (
    app_status_page_html,
    item_page_html,
    month_page_html,
    removed_files_page_html,
)
from bildebank.target_lock import LOCK_FILENAME
from tests.cli_helpers import run_cli
from tests.test_media import minimal_mp4_with_creation_date, minimal_png


class ServerItemActionsCliTests(unittest.TestCase):
    def test_run_server_item_page_has_manual_date_controls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20260102.jpg").write_bytes(b"image")

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
            body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
            )

        self.assertIn("📅", body)
        self.assertIn("Sett manuell dato", body)
        self.assertIn("data-open-manual-date", body)
        self.assertIn('data-manual-date-item="1"', body)
        self.assertIn('id="manualDateOverlay"', body)
        self.assertIn('value="uncertain"', body)
        self.assertIn('value="between"', body)
        self.assertIn("/api/item-manual-date", SERVER_JS)
        self.assertIn("/api/item-manual-date-clear", SERVER_JS)

    def test_run_server_manual_date_endpoint_sets_and_clears_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20260102.jpg").write_bytes(b"image")

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
            data = json.dumps(
                {
                    "file_id": 1,
                    "mode": "uncertain",
                    "date": "2004-07-15",
                    "uncertainty": "1m",
                    "note": "Kamera hadde feil dato",
                }
            ).encode("utf-8")
            response: dict[str, object] = {}

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target)

                def respond_json(
                    self,
                    content: dict[str, object],
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    response["content"] = content
                    response["status"] = status

            handler = FakeHandler()
            server_endpoints_items.respond_manual_date_item(handler)  # type: ignore[arg-type]
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
            )
            info_body = image_info_content_html(target, item)
            original_month_after_set = [
                item["stored_filename"]
                for item in browser_month_items(target, "2026-01")
            ]
            manual_month_after_set = [
                item["stored_filename"]
                for item in browser_month_items(target, "2004-07")
            ]

            clear_data = json.dumps({"file_id": 1}).encode("utf-8")
            clear_response: dict[str, object] = {}

            class ClearHandler:
                headers = {
                    "Content-Length": str(len(clear_data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(clear_data)
                server = SimpleNamespace(target=target)

                def respond_json(
                    self,
                    content: dict[str, object],
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    clear_response["content"] = content
                    clear_response["status"] = status

            clear_handler = ClearHandler()
            server_endpoints_items.respond_clear_manual_date_item(clear_handler)  # type: ignore[arg-type]
            manual_month_after_clear = [
                item["stored_filename"]
                for item in browser_month_items(target, "2004-07")
            ]
            original_month_after_clear = [
                item["stored_filename"]
                for item in browser_month_items(target, "2026-01")
            ]

        self.assertEqual(response["status"], HTTPStatus.OK)
        self.assertEqual(
            response["content"],
            {
                "ok": True,
                "file_id": 1,
                "manual_date_from": "2004-06-15",
                "manual_date_to": "2004-08-15",
            },
        )
        self.assertEqual(original_month_after_set, [])
        self.assertEqual(manual_month_after_set, ["IMG_20260102.jpg"])
        self.assertIn("📅", body)
        self.assertIn("Endre manuell dato", body)
        self.assertIn('data-manual-date-from="2004-06-15"', body)
        self.assertIn('data-manual-date-to="2004-08-15"', body)
        self.assertIn('data-manual-date-note="Kamera hadde feil dato"', body)
        self.assertIn("ca. 2004-07-15", info_body)
        self.assertIn("Kamera hadde feil dato", info_body)
        self.assertEqual(clear_response["status"], HTTPStatus.OK)
        self.assertEqual(clear_response["content"], {"ok": True, "file_id": 1})
        self.assertEqual(manual_month_after_clear, [])
        self.assertEqual(original_month_after_clear, ["IMG_20260102.jpg"])

    def test_run_server_manual_date_endpoint_rejects_invalid_input_without_changing_database(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20260102.jpg").write_bytes(b"image")

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
            data = json.dumps(
                {
                    "file_id": 1,
                    "mode": "between",
                    "date_from": "2004-08-31",
                    "date_to": "2004-06-01",
                    "note": "Skal ikke lagres",
                }
            ).encode("utf-8")
            response: dict[str, object] = {}

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target)

                def respond_json(
                    self,
                    content: dict[str, object],
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    response["content"] = content
                    response["status"] = status

            handler = FakeHandler()
            server_endpoints_items.respond_manual_date_item(handler)  # type: ignore[arg-type]
            conn = db.connect(target)
            try:
                row = conn.execute(
                    "SELECT manual_date_from, manual_date_to, manual_date_note FROM files WHERE id = 1"
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(response["status"], HTTPStatus.BAD_REQUEST)
        self.assertIn("Fra-dato kan ikke være etter til-dato", str(response["content"]))
        self.assertIsNone(row["manual_date_from"])
        self.assertIsNone(row["manual_date_to"])
        self.assertIsNone(row["manual_date_note"])

    def test_run_server_manual_date_endpoints_report_target_lock_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / LOCK_FILENAME).write_text("command=remove\n", encoding="utf-8")

            set_data = json.dumps(
                {"file_id": 1, "mode": "exact", "date": "2004-07-15"}
            ).encode("utf-8")
            clear_data = json.dumps({"file_id": 1}).encode("utf-8")

            class FakeHandler:
                def __init__(self, data: bytes) -> None:
                    self.headers = {
                        "Content-Length": str(len(data)),
                        "Content-Type": "application/json",
                    }
                    self.rfile = BytesIO(data)
                    self.server = SimpleNamespace(target=target)
                    self.body: dict[str, object] | None = None
                    self.status: HTTPStatus | None = None

                def respond_json(
                    self,
                    content: dict[str, object],
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    self.body = content
                    self.status = status

            set_handler = FakeHandler(set_data)
            server_endpoints_items.respond_manual_date_item(set_handler)  # type: ignore[arg-type]
            clear_handler = FakeHandler(clear_data)
            server_endpoints_items.respond_clear_manual_date_item(clear_handler)  # type: ignore[arg-type]

        self.assertEqual(set_handler.status, HTTPStatus.CONFLICT)
        self.assertIn("Bildesamlingen er låst", str(set_handler.body["error"]))
        self.assertEqual(clear_handler.status, HTTPStatus.CONFLICT)
        self.assertIn("Bildesamlingen er låst", str(clear_handler.body["error"]))

    def test_run_server_manual_date_endpoint_rejects_deleted_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20260102.jpg").write_bytes(b"image")

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
            conn = db.connect(target)
            try:
                conn.execute(
                    "UPDATE files SET deleted_at = CURRENT_TIMESTAMP WHERE id = 1"
                )
                conn.commit()
            finally:
                conn.close()
            data = json.dumps(
                {"file_id": 1, "mode": "exact", "date": "2004-07-15"}
            ).encode("utf-8")
            response: dict[str, object] = {}

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target)

                def respond_json(
                    self,
                    content: dict[str, object],
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    response["content"] = content
                    response["status"] = status

            handler = FakeHandler()
            server_endpoints_items.respond_manual_date_item(handler)  # type: ignore[arg-type]

        self.assertEqual(response["status"], HTTPStatus.BAD_REQUEST)
        self.assertIn("Filen er markert som slettet", str(response["content"]))

    def test_run_server_hotkey_action_is_rejected_when_hints_are_hidden(self) -> None:
        data = json.dumps({"file_id": 1, "key": "5"}).encode("utf-8")
        response: dict[str, object] = {}

        class FakeHandler:
            headers = {
                "Content-Length": str(len(data)),
                "Content-Type": "application/json",
            }
            rfile = BytesIO(data)
            server = SimpleNamespace(
                config=AppConfig(browser=BrowserConfig(hotkey_hints_enabled=False))
            )

            def respond_json(
                self, content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK
            ) -> None:
                response["content"] = content
                response["status"] = status

        server_endpoints_items.respond_hotkey_action(FakeHandler())  # type: ignore[arg-type]

        self.assertEqual(response["status"], HTTPStatus.FORBIDDEN)
        self.assertEqual(
            response["content"], {"ok": False, "error": "Hurtigtaster er slått av."}
        )

    def test_run_server_hotkey_action_sets_manual_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20260102.jpg").write_bytes(b"image")

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
            data = json.dumps({"file_id": 1, "key": "5"}).encode("utf-8")
            response: dict[str, object] = {}
            hotkeys = {
                "5": BrowserHotkeyConfig(
                    action="manual_date",
                    mode="between",
                    date_from="2004-06-01",
                    date_to="2004-08-31",
                    note="Sommer 2004",
                )
            }

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    target=target,
                    config=AppConfig(
                        browser=BrowserConfig(
                            hotkey_hints_enabled=True, hotkeys=hotkeys
                        )
                    ),
                )

                def respond_json(
                    self,
                    content: dict[str, object],
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    response["content"] = content
                    response["status"] = status

            handler = FakeHandler()
            server_endpoints_items.respond_hotkey_action(handler)  # type: ignore[arg-type]
            item = browser_item_by_id(target, 1)

        self.assertEqual(response["status"], HTTPStatus.OK)
        self.assertEqual(response["content"]["action"], "manual_date")
        self.assertEqual(response["content"]["manual_date_from"], "2004-06-01")
        self.assertEqual(response["content"]["manual_date_to"], "2004-08-31")
        self.assertEqual(item["manual_date_note"], "Sommer 2004")

    def test_run_server_hotkey_action_adds_person_to_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20260102.jpg").write_bytes(b"image")

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
            create_person(target, "Kari")
            data = json.dumps({"file_id": 1, "key": "3"}).encode("utf-8")
            response: dict[str, object] = {}
            hotkeys = {"3": BrowserHotkeyConfig(action="person", person_name="Kari")}

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    target=target,
                    face_enabled=True,
                    config=AppConfig(
                        browser=BrowserConfig(
                            hotkey_hints_enabled=True, hotkeys=hotkeys
                        )
                    ),
                )

                def respond_json(
                    self,
                    content: dict[str, object],
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    response["content"] = content
                    response["status"] = status

            handler = FakeHandler()
            server_endpoints_items.respond_hotkey_action(handler)  # type: ignore[arg-type]
            face_conn = connect_face_db(target)
            try:
                manual_link_count = face_conn.execute(
                    "SELECT COUNT(*) FROM person_files"
                ).fetchone()[0]
            finally:
                face_conn.close()

        self.assertEqual(response["status"], HTTPStatus.OK)
        self.assertEqual(response["content"]["action"], "person")
        self.assertEqual(response["content"]["person_name"], "Kari")
        self.assertEqual(response["content"]["confirmed"], True)
        self.assertEqual(manual_link_count, 1)

    def test_run_server_hotkey_action_sets_tag_on_file_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20260102.jpg").write_bytes(b"image")

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
            data = json.dumps({"file_id": 1, "key": "2"}).encode("utf-8")
            responses: list[dict[str, object]] = []
            tag_navigation_changes: list[str] = []
            hotkeys = {"2": BrowserHotkeyConfig(action="tag", tag_name="  Familie  ")}

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                server = SimpleNamespace(
                    target=target,
                    config=AppConfig(
                        browser=BrowserConfig(
                            hotkey_hints_enabled=True, hotkeys=hotkeys
                        )
                    ),
                )

                def __init__(self) -> None:
                    self.rfile = BytesIO(data)

                def respond_json(
                    self,
                    content: dict[str, object],
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    responses.append({"content": content, "status": status})

                server.note_tag_navigation_change = tag_navigation_changes.append

            server_endpoints_items.respond_hotkey_action(FakeHandler())  # type: ignore[arg-type]
            server_endpoints_items.respond_hotkey_action(FakeHandler())  # type: ignore[arg-type]
            conn = db.connect(target)
            try:
                tag_row = conn.execute(
                    "SELECT id, name FROM tags WHERE name_key = ?", ("familie",)
                ).fetchone()
                link_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM file_tags WHERE file_id = 1"
                ).fetchone()["count"]
            finally:
                conn.close()

        self.assertEqual(responses[0]["status"], HTTPStatus.OK)
        self.assertEqual(responses[0]["content"]["action"], "tag")
        self.assertEqual(responses[0]["content"]["tag_name"], "Familie")
        self.assertEqual(responses[0]["content"]["tagged"], True)
        self.assertEqual(responses[1]["status"], HTTPStatus.OK)
        self.assertEqual(responses[1]["content"]["tag_name"], "Familie")
        self.assertEqual(tag_row["name"], "Familie")
        self.assertEqual(link_count, 1)
        self.assertEqual(tag_navigation_changes, ["Familie", "Familie"])

    def test_run_server_item_page_has_delete_button(self) -> None:
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
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
            )

        self.assertIn("Slett", body)
        self.assertIn('data-delete-item="1"', body)
        self.assertIn('data-delete-path="2024/01/IMG_20240102.png"', body)
        self.assertIn("/api/item-delete", SERVER_JS)
        self.assertIn("Flytte til deleted/?", SERVER_JS)

    def test_run_server_item_page_omits_legacy_manual_location_button(self) -> None:
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
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
            )

        nav_start = body.index('<nav class="controls"')
        tag_rail_start = body.index('<aside class="tag-rail"')
        nav_body = body[nav_start : body.index("</nav>", nav_start)]
        tag_rail_body = body[tag_rail_start : body.index("</aside>", tag_rail_start)]
        self.assertNotIn("Sett valgt H3-celle", tag_rail_body)
        self.assertNotIn("Sett sted", body)
        self.assertNotIn("Sett valgt H3-celle", nav_body)
        self.assertNotIn('data-manual-location-item="1"', body)
        self.assertNotIn("data-manual-location-cell", body)
        self.assertNotIn('fetch("/api/item-manual-location"', SERVER_JS)
        self.assertIn("/api/item-manual-location-remove", SERVER_JS)
        self.assertIn("/api/item-hotkey-action", SERVER_JS)
        self.assertIn('["1", "2", "3", "4", "5"].includes(event.key)', SERVER_JS)
        self.assertNotIn('event.key.toLowerCase() === "g"', SERVER_JS)
        self.assertNotIn("setManualLocation(button)", SERVER_JS)
        self.assertNotIn("Sette sted fra aktiv H3-celle?", SERVER_JS)
        self.assertIn('data-browser-item-id="1"', body)

    def test_run_server_item_page_has_manual_h3_picker_for_items_without_real_gps(
        self,
    ) -> None:
        h3_cell = h3_cells_for_point(59.91273, 10.74609)["h3_res7"]
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
            conn = db.connect(target)
            try:
                db.set_geo_place_name(conn, h3_cell, "Brevik")
                conn.commit()
            finally:
                conn.close()
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
            )

        self.assertIn("data-open-manual-h3", body)
        self.assertIn('data-manual-h3-item="1"', body)
        self.assertIn("🌐</button>", body)
        self.assertIn('id="manualH3Overlay"', body)
        self.assertIn('data-manual-h3-panel data-file-id="1"', body)
        self.assertIn('data-manual-h3-cell="' + h3_cell + '"', body)
        self.assertIn("Brevik", body)
        self.assertIn("res 7", body)
        self.assertIn("/api/item-manual-location", SERVER_JS)

    def test_run_server_item_page_marks_current_manual_h3_cell_in_picker(self) -> None:
        h3_cell = h3_cells_for_point(59.91273, 10.74609)["h3_res7"]
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
            conn = db.connect(target)
            try:
                db.set_geo_place_name(conn, h3_cell, "Brevik")
                db.set_file_manual_h3_location(
                    conn, file_id=1, h3_cells={"h3_res7": h3_cell}
                )
                conn.commit()
            finally:
                conn.close()
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
            )

        self.assertIn('title="Endre manuelt sted"', body)
        self.assertIn('class="manual-h3-option active"', body)
        self.assertIn('class="manual-h3-current">valgt</span>', body)

    def test_run_server_item_page_hides_manual_h3_picker_for_real_gps_or_no_named_cells(
        self,
    ) -> None:
        h3_cell = h3_cells_for_point(59.91273, 10.74609)["h3_res7"]
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
            item_without_places = browser_item_by_id(target, 1)
            self.assertIsNotNone(item_without_places)
            body_without_places = item_page_html(
                target,
                item_without_places,
                *adjacent_browser_items(target, item_without_places),
                browser_month_navigation(target, item_without_places),
            )
            conn = db.connect(target)
            try:
                db.set_geo_place_name(conn, h3_cell, "Brevik")
                conn.execute(
                    "UPDATE files SET gps_lat = 59.91273, gps_lon = 10.74609, gps_source = 'exiftool' WHERE id = 1"
                )
                conn.commit()
            finally:
                conn.close()
            item_with_gps = browser_item_by_id(target, 1)
            self.assertIsNotNone(item_with_gps)
            body_with_gps = item_page_html(
                target,
                item_with_gps,
                *adjacent_browser_items(target, item_with_gps),
                browser_month_navigation(target, item_with_gps),
            )

        self.assertNotIn("data-open-manual-h3", body_without_places)
        self.assertNotIn('id="manualH3Overlay"', body_without_places)
        self.assertNotIn("data-open-manual-h3", body_with_gps)
        self.assertNotIn('id="manualH3Overlay"', body_with_gps)

    def test_run_server_item_page_can_show_hotkey_hints_in_tag_rail(self) -> None:
        h3_cell = h3_cells_for_point(59.91273, 10.74609)["h3_res7"]
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
            conn = db.connect(target)
            try:
                db.set_geo_place_name(conn, h3_cell, "Brevik")
                conn.commit()
            finally:
                conn.close()
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            hotkeys = {
                "1": BrowserHotkeyConfig(action="h3", h3_cell=h3_cell),
                "3": BrowserHotkeyConfig(action="person", person_name="Viljar"),
                "4": BrowserHotkeyConfig(action="tag", tag_name="Familie"),
                "5": BrowserHotkeyConfig(
                    action="manual_date",
                    mode="uncertain",
                    date="1948-12-30",
                    uncertainty="1w",
                ),
            }
            body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
                face_enabled=False,
                hotkey_hints_enabled=True,
                hotkeys=hotkeys,
            )
            hidden_body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
                face_enabled=False,
                hotkey_hints_enabled=False,
                hotkeys=hotkeys,
            )

        tag_rail_start = body.index('<aside class="tag-rail"')
        tag_rail_body = body[tag_rail_start : body.index("</aside>", tag_rail_start)]
        self.assertIn('class="hotkey-hints"', tag_rail_body)
        self.assertIn(
            '<div class="hotkey-hints-heading">Hurtigtaster aktivert:</div>',
            tag_rail_body,
        )
        self.assertIn("<span>1:</span> Sett H3 til Brevik", tag_rail_body)
        self.assertIn("<span>3:</span> Legg til Viljar", tag_rail_body)
        self.assertIn("<span>4:</span> Sett tagg Familie", tag_rail_body)
        self.assertIn("<span>5:</span> Sett dato til 30.12.48 ±1w", tag_rail_body)
        self.assertNotIn("date-status-badge", tag_rail_body)
        self.assertLess(
            tag_rail_body.index("location-status-badge"),
            tag_rail_body.index('class="hotkey-hints"'),
        )
        self.assertTrue(tag_rail_body.strip().endswith("</section>"))
        self.assertIn('data-browser-hotkeys-enabled="true"', body)
        self.assertNotIn("data-browser-hotkeys-enabled", hidden_body)
        self.assertIn('itemRoot?.dataset.browserHotkeysEnabled === "true"', SERVER_JS)
        self.assertIn('itemRoot?.dataset.browserHotkeysEnabled !== "true"', SERVER_JS)
        self.assertNotIn('class="hotkey-hints"', hidden_body)

    def test_run_server_hotkey_action_sets_h3_location(self) -> None:
        import h3

        h3_cell = h3.latlng_to_cell(59.91273, 10.74609, 3)
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
            data = json.dumps({"file_id": 1, "key": "1"}).encode("utf-8")
            hotkeys = {"1": BrowserHotkeyConfig(action="h3", h3_cell=h3_cell)}

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    target=target,
                    face_enabled=True,
                    config=AppConfig(
                        browser=BrowserConfig(
                            hotkey_hints_enabled=True, hotkeys=hotkeys
                        )
                    ),
                )
                body: dict[str, object] | None = None
                timings: list[str] = []

                def respond_json(
                    self, content: dict[str, object], *, status=None
                ) -> None:
                    self.body = content

                def record_server_timing(self, name: str, start: float) -> None:
                    self.timings.append(name)

            handler = FakeHandler()
            server_endpoints_items.respond_hotkey_action(handler)  # type: ignore[arg-type]

            conn = db.connect(target)
            try:
                db.set_file_tag(
                    conn, file_id=1, tag_name=db.SYSTEM_TAG_OUT_OF_FOCUS, tagged=True
                )
                conn.commit()
            finally:
                conn.close()
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
            )
            info_body = image_info_content_html(target, item)
            conn = db.connect(target)
            try:
                row = conn.execute(
                    "SELECT gps_lat, gps_lon, gps_alt, gps_source, gps_error, h3_res0, h3_res3, h3_res4, h3_res11 FROM files WHERE id = 1"
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(
            handler.body,
            {
                "ok": True,
                "key": "1",
                "action": "h3",
                "file_id": 1,
                "gps_source": "manual-h3",
            },
        )
        self.assertEqual(
            handler.timings,
            [
                "hotkey_read_payload",
                "hotkey_validate",
                "hotkey_filter_parse",
                "hotkey_apply",
                "hotkey_post_apply",
            ],
        )
        self.assertEqual(row["gps_source"], "manual-h3")
        self.assertIsNone(row["gps_alt"])
        self.assertIsNone(row["gps_error"])
        self.assertIsNone(row["gps_lat"])
        self.assertIsNone(row["gps_lon"])
        self.assertTrue(row["h3_res0"])
        self.assertEqual(row["h3_res3"], h3_cell)
        self.assertIsNone(row["h3_res4"])
        self.assertIsNone(row["h3_res11"])
        self.assertIn('<aside class="tag-rail"', body)
        self.assertIn(
            f'href="https://h3geo.org/#hex={h3_cell}" target="_blank" title="Vis plasseringen på kartet på https://h3geo.org/" rel="noopener">Manuell H3</a>',
            body,
        )
        self.assertNotIn("Fjern manuelt sted", body)
        self.assertIn('class="inline-link danger-inline-link"', body)
        self.assertIn(
            'Manuell H3</a><span class="manual-location-remove">(<button class="inline-link danger-inline-link"',
            body,
        )
        self.assertIn(">fjern</button>)</span>", body)
        self.assertIn('data-remove-manual-location-item="1"', body)
        self.assertNotIn('data-manual-location-item="1"', body)
        self.assertNotIn("Sett valgt H3-celle", body)
        self.assertNotIn("<dt>Kart</dt>", info_body)
        self.assertIn("Manuell H3", info_body)
        self.assertIn(f'href="https://h3geo.org/#hex={h3_cell}"', info_body)
        self.assertLess(
            info_body.index("<dt>Tagger</dt>"), info_body.index("Manuell H3")
        )
        self.assertIn("<dt>GPS-kilde</dt>", info_body)
        self.assertIn("satt manuelt", info_body)

    def test_run_server_hotkey_action_redirects_to_next_filter_item_when_current_no_longer_matches(
        self,
    ) -> None:
        import h3

        h3_cell = h3.latlng_to_cell(59.91273, 10.74609, 3)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240101.png").write_bytes(minimal_png(100, 80))
            (source / "IMG_20240102.png").write_bytes(minimal_png(101, 80))

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
            filter_source = text_filter_browser_source("missing:gps", target)
            data = json.dumps(
                {"file_id": 1, "key": "1", "source_url": filter_source.root_url}
            ).encode("utf-8")
            hotkeys = {"1": BrowserHotkeyConfig(action="h3", h3_cell=h3_cell)}

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    target=target,
                    face_enabled=True,
                    config=AppConfig(
                        browser=BrowserConfig(
                            hotkey_hints_enabled=True, hotkeys=hotkeys
                        )
                    ),
                )
                body: dict[str, object] | None = None

                def respond_json(
                    self,
                    content: dict[str, object],
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    self.body = content

            handler = FakeHandler()
            server_endpoints_items.respond_hotkey_action(handler)  # type: ignore[arg-type]
            row = browser_item_by_id(target, 1)

        self.assertEqual(handler.body["action"], "h3")
        self.assertEqual(handler.body["gps_source"], "manual-h3")
        self.assertEqual(handler.body["redirect_url"], "/filter/missing%3Agps/item/2")
        self.assertEqual(row["gps_source"], "manual-h3")

    def test_run_server_manual_h3_hotkey_reports_target_lock_conflict(self) -> None:
        import h3

        h3_cell = h3.latlng_to_cell(59.91273, 10.74609, 3)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240101.png").write_bytes(minimal_png(100, 80))

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
            (target / LOCK_FILENAME).write_text("command=geo-scan\n", encoding="utf-8")
            data = json.dumps({"file_id": 1, "key": "1"}).encode("utf-8")
            hotkeys = {"1": BrowserHotkeyConfig(action="h3", h3_cell=h3_cell)}

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    target=target,
                    face_enabled=True,
                    config=AppConfig(
                        browser=BrowserConfig(
                            hotkey_hints_enabled=True, hotkeys=hotkeys
                        )
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
            server_endpoints_items.respond_hotkey_action(handler)  # type: ignore[arg-type]
            row = browser_item_by_id(target, 1)

        self.assertEqual(handler.status, HTTPStatus.CONFLICT)
        self.assertIn("Bildesamlingen er låst", str(handler.body["error"]))
        self.assertIsNone(row["gps_source"])

    def test_run_server_hotkey_action_redirects_to_previous_filter_item_from_last_match(
        self,
    ) -> None:
        import h3

        h3_cell = h3.latlng_to_cell(59.91273, 10.74609, 3)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240101.png").write_bytes(minimal_png(100, 80))
            (source / "IMG_20240102.png").write_bytes(minimal_png(101, 80))

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
            filter_source = text_filter_browser_source("missing:gps", target)
            data = json.dumps(
                {"file_id": 2, "key": "1", "source_url": filter_source.root_url}
            ).encode("utf-8")
            hotkeys = {"1": BrowserHotkeyConfig(action="h3", h3_cell=h3_cell)}

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    target=target,
                    face_enabled=True,
                    config=AppConfig(
                        browser=BrowserConfig(
                            hotkey_hints_enabled=True, hotkeys=hotkeys
                        )
                    ),
                )
                body: dict[str, object] | None = None

                def respond_json(
                    self,
                    content: dict[str, object],
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    self.body = content

            handler = FakeHandler()
            server_endpoints_items.respond_hotkey_action(handler)  # type: ignore[arg-type]

        self.assertEqual(handler.body["redirect_url"], "/filter/missing%3Agps/item/1")

    def test_run_server_hotkey_action_redirects_to_filter_root_when_filter_becomes_empty(
        self,
    ) -> None:
        import h3

        h3_cell = h3.latlng_to_cell(59.91273, 10.74609, 3)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240101.png").write_bytes(minimal_png(100, 80))

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
            filter_source = text_filter_browser_source("missing:gps", target)
            data = json.dumps(
                {"file_id": 1, "key": "1", "source_url": filter_source.root_url}
            ).encode("utf-8")
            hotkeys = {"1": BrowserHotkeyConfig(action="h3", h3_cell=h3_cell)}

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    target=target,
                    face_enabled=True,
                    config=AppConfig(
                        browser=BrowserConfig(
                            hotkey_hints_enabled=True, hotkeys=hotkeys
                        )
                    ),
                )
                body: dict[str, object] | None = None

                def respond_json(
                    self,
                    content: dict[str, object],
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    self.body = content

            handler = FakeHandler()
            server_endpoints_items.respond_hotkey_action(handler)  # type: ignore[arg-type]

        self.assertEqual(handler.body["redirect_url"], "/filter/missing%3Agps")

    def test_run_server_manual_h3_endpoint_sets_location(self) -> None:
        import h3

        h3_cell = h3.latlng_to_cell(59.91273, 10.74609, 3)
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
            data = json.dumps({"file_id": 1, "h3_cell": h3_cell}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target)
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
            server_endpoints_items.respond_manual_location_item(handler)  # type: ignore[arg-type]
            conn = db.connect(target)
            try:
                row = conn.execute(
                    "SELECT gps_lat, gps_lon, gps_alt, gps_source, gps_error, h3_res0, h3_res3, h3_res4 FROM files WHERE id = 1"
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(
            handler.body,
            {"ok": True, "file_id": 1, "gps_source": "manual-h3", "h3_cell": h3_cell},
        )
        self.assertIsNone(row["gps_lat"])
        self.assertIsNone(row["gps_lon"])
        self.assertIsNone(row["gps_alt"])
        self.assertIsNone(row["gps_error"])
        self.assertEqual(row["gps_source"], "manual-h3")
        self.assertTrue(row["h3_res0"])
        self.assertEqual(row["h3_res3"], h3_cell)
        self.assertIsNone(row["h3_res4"])

    def test_run_server_manual_h3_endpoint_rejects_invalid_inputs(self) -> None:
        h3_cell = h3_cells_for_point(59.91273, 10.74609)["h3_res7"]
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

            def post(
                payload: dict[str, object],
            ) -> tuple[dict[str, object], HTTPStatus]:
                data = json.dumps(payload).encode("utf-8")

                class FakeHandler:
                    headers = {
                        "Content-Length": str(len(data)),
                        "Content-Type": "application/json",
                    }
                    rfile = BytesIO(data)
                    server = SimpleNamespace(target=target)
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
                server_endpoints_items.respond_manual_location_item(handler)  # type: ignore[arg-type]
                assert handler.body is not None
                assert handler.status is not None
                return handler.body, handler.status

            invalid_file, invalid_file_status = post(
                {"file_id": "x", "h3_cell": h3_cell}
            )
            missing_file, missing_file_status = post(
                {"file_id": 999, "h3_cell": h3_cell}
            )
            empty_cell, empty_cell_status = post({"file_id": 1, "h3_cell": ""})
            invalid_cell, invalid_cell_status = post(
                {"file_id": 1, "h3_cell": "ikke-h3"}
            )
            conn = db.connect(target)
            try:
                conn.execute(
                    "UPDATE files SET gps_lat = 59.91273, gps_lon = 10.74609, gps_source = 'exiftool' WHERE id = 1"
                )
                conn.commit()
            finally:
                conn.close()
            gps_file, gps_file_status = post({"file_id": 1, "h3_cell": h3_cell})

        self.assertEqual(invalid_file_status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(invalid_file["error"], "Ugyldig file_id.")
        self.assertEqual(missing_file_status, HTTPStatus.BAD_REQUEST)
        self.assertIn("Filen finnes ikke", str(missing_file["error"]))
        self.assertEqual(empty_cell_status, HTTPStatus.BAD_REQUEST)
        self.assertIn("Aktiv manuell H3-celle", str(empty_cell["error"]))
        self.assertEqual(invalid_cell_status, HTTPStatus.BAD_REQUEST)
        self.assertIn("Ugyldig H3-celle", str(invalid_cell["error"]))
        self.assertEqual(gps_file_status, HTTPStatus.BAD_REQUEST)
        self.assertIn("Filen har GPS-lokasjon", str(gps_file["error"]))

    def test_run_server_manual_h3_endpoint_rejects_deleted_file_and_target_lock(
        self,
    ) -> None:
        import h3

        h3_cell = h3.latlng_to_cell(59.91273, 10.74609, 3)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(100, 80))
            (source / "IMG_20240103.png").write_bytes(minimal_png(101, 80))

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
            conn = db.connect(target)
            try:
                conn.execute(
                    "UPDATE files SET deleted_at = CURRENT_TIMESTAMP WHERE id = 1"
                )
                conn.commit()
            finally:
                conn.close()

            def post(file_id: int) -> tuple[dict[str, object], HTTPStatus]:
                data = json.dumps({"file_id": file_id, "h3_cell": h3_cell}).encode(
                    "utf-8"
                )

                class FakeHandler:
                    headers = {
                        "Content-Length": str(len(data)),
                        "Content-Type": "application/json",
                    }
                    rfile = BytesIO(data)
                    server = SimpleNamespace(target=target)
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
                server_endpoints_items.respond_manual_location_item(handler)  # type: ignore[arg-type]
                assert handler.body is not None
                assert handler.status is not None
                return handler.body, handler.status

            deleted_file, deleted_status = post(1)
            (target / LOCK_FILENAME).write_text("command=geo-scan\n", encoding="utf-8")
            locked_file, locked_status = post(2)

        self.assertEqual(deleted_status, HTTPStatus.BAD_REQUEST)
        self.assertIn("Filen er markert som slettet", str(deleted_file["error"]))
        self.assertEqual(locked_status, HTTPStatus.CONFLICT)
        self.assertIn("Bildesamlingen er låst", str(locked_file["error"]))

    def test_run_server_manual_h3_endpoint_redirects_when_filter_no_longer_matches(
        self,
    ) -> None:
        import h3

        h3_cell = h3.latlng_to_cell(59.91273, 10.74609, 3)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240101.png").write_bytes(minimal_png(100, 80))
            (source / "IMG_20240102.png").write_bytes(minimal_png(101, 80))

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
            filter_source = text_filter_browser_source("missing:gps", target)
            data = json.dumps(
                {"file_id": 1, "h3_cell": h3_cell, "source_url": filter_source.root_url}
            ).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target)
                body: dict[str, object] | None = None

                def respond_json(
                    self,
                    content: dict[str, object],
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    self.body = content

            handler = FakeHandler()
            server_endpoints_items.respond_manual_location_item(handler)  # type: ignore[arg-type]

        self.assertEqual(handler.body["gps_source"], "manual-h3")
        self.assertEqual(handler.body["redirect_url"], "/filter/missing%3Agps/item/2")

    def test_run_server_item_manual_location_remove_endpoint_clears_manual_h3(
        self,
    ) -> None:
        import h3

        h3_cell = h3.latlng_to_cell(59.91273, 10.74609, 3)
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
            conn = db.connect(target)
            try:
                db.set_file_manual_h3_location(
                    conn,
                    file_id=1,
                    h3_cells={
                        "h3_res0": h3.cell_to_parent(h3_cell, 0),
                        "h3_res3": h3_cell,
                    },
                )
                conn.commit()
            finally:
                conn.close()
            data = json.dumps({"file_id": 1}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target)
                body: dict[str, object] | None = None

                def respond_json(
                    self,
                    content: dict[str, object],
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    self.body = content

            handler = FakeHandler()
            server_endpoints_items.respond_remove_manual_location_item(handler)  # type: ignore[arg-type]
            conn = db.connect(target)
            try:
                row = conn.execute(
                    "SELECT gps_source, gps_scanned_at, h3_res0, h3_res3 FROM files WHERE id = 1"
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(handler.body, {"ok": True, "file_id": 1, "gps_source": None})
        self.assertIsNone(row["gps_source"])
        self.assertIsNone(row["gps_scanned_at"])
        self.assertIsNone(row["h3_res0"])
        self.assertIsNone(row["h3_res3"])

    def test_run_server_manual_location_remove_reports_target_lock_conflict(
        self,
    ) -> None:
        import h3

        h3_cell = h3.latlng_to_cell(59.91273, 10.74609, 3)
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
            conn = db.connect(target)
            try:
                db.set_file_manual_h3_location(
                    conn,
                    file_id=1,
                    h3_cells={
                        "h3_res0": h3.cell_to_parent(h3_cell, 0),
                        "h3_res3": h3_cell,
                    },
                )
                conn.commit()
            finally:
                conn.close()
            (target / LOCK_FILENAME).write_text("command=geo-scan\n", encoding="utf-8")
            data = json.dumps({"file_id": 1}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target)
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
            server_endpoints_items.respond_remove_manual_location_item(handler)  # type: ignore[arg-type]
            row = browser_item_by_id(target, 1)

        self.assertEqual(handler.status, HTTPStatus.CONFLICT)
        self.assertIn("Bildesamlingen er låst", str(handler.body["error"]))
        self.assertEqual(row["gps_source"], "manual-h3")

    def test_run_server_item_page_lists_defined_tags_before_geo_info(self) -> None:
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
            conn = db.connect(target)
            try:
                db.ensure_tag(conn, "Familie")
                conn.commit()
            finally:
                conn.close()
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
            )

        self.assertIn('class="tag-rail"', body)
        self.assertIn('data-tag-toggle="1"', body)
        self.assertIn('data-tag-name="Ute av fokus"', body)
        self.assertIn('data-tag-name="Familie"', body)
        self.assertIn('aria-pressed="false"', body)
        self.assertLess(
            body.index('data-tag-name="Ute av fokus"'),
            body.index('data-tag-name="Familie"'),
        )
        self.assertNotIn('class="date-status-badge"', body)
        self.assertLess(
            body.index('data-tag-name="Familie"'),
            body.index('class="location-status-badge"'),
        )
        self.assertIn("/api/item-tag", SERVER_JS)
        tag_handler = SERVER_JS[
            SERVER_JS.index(
                'document.querySelectorAll("[data-tag-toggle]'
            ) : SERVER_JS.index("function updateHotkeyForm")
        ]
        self.assertIn(
            'button.classList.toggle("active", Boolean(payload.tagged));', tag_handler
        )
        self.assertNotIn("window.location.reload();", tag_handler)
        self.assertIn("stage-shell", SERVER_CSS)
        self.assertIn("tag-rail", SERVER_CSS)
        self.assertIn(".tag-toggle::before", SERVER_CSS)
        self.assertIn(".tag-toggle.active::before", SERVER_CSS)

    def test_run_server_item_tag_endpoint_sets_system_tag(self) -> None:
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
            data = json.dumps(
                {"file_id": 1, "tag_name": db.SYSTEM_TAG_OUT_OF_FOCUS, "tagged": True}
            ).encode("utf-8")

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
                timings: list[str] = []
                tag_navigation_changes: list[str] = []

                def respond_json(
                    self, content: dict[str, object], *, status=None
                ) -> None:
                    self.body = content

                def record_server_timing(self, name: str, start: float) -> None:
                    self.timings.append(name)

                def __init__(self) -> None:
                    self.server.note_tag_navigation_change = (
                        self.tag_navigation_changes.append
                    )

            handler = FakeHandler()
            server_endpoints_items.respond_tag_item(handler)  # type: ignore[arg-type]
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
            )
            info_body = image_info_content_html(target, item)

        self.assertEqual(
            {
                "ok": True,
                "file_id": 1,
                "tag_name": db.SYSTEM_TAG_OUT_OF_FOCUS,
                "tagged": True,
            },
            handler.body,
        )
        self.assertEqual(
            handler.timings, ["tag_read_payload", "tag_validate", "tag_apply"]
        )
        self.assertEqual(handler.tag_navigation_changes, [db.SYSTEM_TAG_OUT_OF_FOCUS])
        self.assertIn('class="tag-toggle active"', body)
        self.assertIn('aria-pressed="true"', body)
        self.assertIn("Ute av fokus", info_body)
        self.assertIn("(system)", info_body)

    def test_run_server_item_tag_endpoint_sets_user_tag(self) -> None:
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
            data = json.dumps(
                {"file_id": 1, "tag_name": "Familie", "tagged": True}
            ).encode("utf-8")

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
                tag_navigation_changes: list[str] = []

                def respond_json(
                    self, content: dict[str, object], *, status=None
                ) -> None:
                    self.body = content
                    self.status = status

                def __init__(self) -> None:
                    self.server.note_tag_navigation_change = (
                        self.tag_navigation_changes.append
                    )

            handler = FakeHandler()
            server_endpoints_items.respond_tag_item(handler)  # type: ignore[arg-type]
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
            )
            info_body = image_info_content_html(target, item)

        self.assertIsNone(handler.status)
        self.assertEqual(
            {"ok": True, "file_id": 1, "tag_name": "Familie", "tagged": True},
            handler.body,
        )
        self.assertEqual(handler.tag_navigation_changes, ["Familie"])
        self.assertIn('data-tag-name="Familie" aria-pressed="true"', body)
        self.assertIn("Familie", info_body)

    def test_run_server_item_tag_returns_conflict_when_target_is_locked(self) -> None:
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
            lock_path = target / LOCK_FILENAME
            lock_path.write_text("command=remove\npid=123\n", encoding="utf-8")
            data = json.dumps(
                {"file_id": 1, "tag_name": "Familie", "tagged": True}
            ).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target)
                body: dict[str, object] | None = None
                status = None

                def respond_json(
                    self, content: dict[str, object], *, status=None
                ) -> None:
                    self.body = content
                    self.status = status

            handler = FakeHandler()
            server_endpoints_items.respond_tag_item(handler)  # type: ignore[arg-type]

            self.assertEqual(HTTPStatus.CONFLICT, handler.status)
            self.assertEqual(False, handler.body["ok"])
            self.assertIn("Bildesamlingen er låst", str(handler.body["error"]))
            self.assertTrue(lock_path.exists())
            conn = db.connect(target)
            try:
                self.assertIsNone(
                    conn.execute(
                        "SELECT id FROM tags WHERE name_key = 'familie'"
                    ).fetchone()
                )
            finally:
                conn.close()

    def test_run_server_delete_button_moves_file_to_deleted(self) -> None:
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
            original = target / "2024" / "01" / "IMG_20240102.png"
            deleted = target / "deleted" / "2024" / "01" / "IMG_20240102.png"
            original_bytes = original.read_bytes()
            data = json.dumps({"file_id": 1}).encode("utf-8")

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
            server_endpoints_items.respond_delete_item(handler)  # type: ignore[arg-type]
            conn = sqlite3.connect(target / DB_FILENAME)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT target_path, deleted_at, deleted_original_target_path FROM files WHERE id = 1"
                ).fetchone()
            finally:
                conn.close()

            self.assertEqual(
                {
                    "ok": True,
                    "file_id": 1,
                    "deleted_path": "deleted/2024/01/IMG_20240102.png",
                },
                handler.body,
            )
            self.assertFalse(original.exists())
            self.assertTrue(deleted.exists())
            self.assertEqual(deleted.read_bytes(), original_bytes)
            self.assertEqual(row["target_path"], "deleted/2024/01/IMG_20240102.png")
            self.assertEqual(
                row["deleted_original_target_path"], "2024/01/IMG_20240102.png"
            )
            self.assertIsNotNone(row["deleted_at"])
            self.assertIsNone(browser_item_by_id(target, 1))

    def test_run_server_delete_returns_conflict_when_target_is_locked(self) -> None:
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
            original = target / "2024" / "01" / "IMG_20240102.png"
            (target / LOCK_FILENAME).write_text(
                "command=import\npid=123\n", encoding="utf-8"
            )
            data = json.dumps({"file_id": 1}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target)
                body: dict[str, object] | None = None
                status = None

                def respond_json(
                    self, content: dict[str, object], *, status=None
                ) -> None:
                    self.body = content
                    self.status = status

            handler = FakeHandler()
            server_endpoints_items.respond_delete_item(handler)  # type: ignore[arg-type]

            self.assertEqual(HTTPStatus.CONFLICT, handler.status)
            self.assertEqual(False, handler.body["ok"])
            self.assertIn("Bildesamlingen er låst", str(handler.body["error"]))
            self.assertTrue(original.exists())

    def test_app_links_to_removed_files_page(self) -> None:
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
            self.assertEqual(
                run_cli(
                    ["--target", str(target), "remove", "2024/01/IMG_20240102.png"]
                ),
                0,
            )

            app_body = app_status_page_html(target)
            removed_body = removed_files_page_html(target)

        self.assertIn('href="/settings/removed"', app_body)
        self.assertIn("Slettede bilder", removed_body)
        self.assertIn('href="/file/1"', removed_body)
        self.assertIn("2024/01/IMG_20240102.png", removed_body)
        self.assertIn('data-undelete-item="1"', removed_body)
        self.assertIn("/api/item-undelete", SERVER_JS)

    def test_run_server_undelete_restores_file(self) -> None:
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
            self.assertEqual(
                run_cli(
                    ["--target", str(target), "remove", "2024/01/IMG_20240102.png"]
                ),
                0,
            )
            restored = undelete_file_from_browser(target, 1)

            conn = sqlite3.connect(target / DB_FILENAME)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT target_path, deleted_at, deleted_original_target_path FROM files WHERE id = 1"
                ).fetchone()
            finally:
                conn.close()

            self.assertEqual(restored.as_posix(), "2024/01/IMG_20240102.png")
            self.assertEqual(row["target_path"], "2024/01/IMG_20240102.png")
            self.assertIsNone(row["deleted_at"])
            self.assertIsNone(row["deleted_original_target_path"])
            self.assertIsNotNone(browser_item_by_id(target, 1))

    def test_run_server_undelete_endpoint_restores_file(self) -> None:
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
            self.assertEqual(
                run_cli(
                    ["--target", str(target), "remove", "2024/01/IMG_20240102.png"]
                ),
                0,
            )
            data = json.dumps({"file_id": 1}).encode("utf-8")

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
            server_endpoints_items.respond_undelete_item(handler)  # type: ignore[arg-type]

            self.assertEqual(
                {"ok": True, "file_id": 1, "restored_path": "2024/01/IMG_20240102.png"},
                handler.body,
            )
            self.assertTrue((target / "2024" / "01" / "IMG_20240102.png").exists())
            self.assertFalse(
                (target / "deleted" / "2024" / "01" / "IMG_20240102.png").exists()
            )

    def test_run_server_undelete_returns_conflict_when_target_is_locked(self) -> None:
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
            self.assertEqual(
                run_cli(
                    [
                        "--target",
                        str(target),
                        "remove",
                        "2024/01/IMG_20240102.png",
                    ]
                ),
                0,
            )
            deleted = target / "deleted" / "2024" / "01" / "IMG_20240102.png"
            (target / LOCK_FILENAME).write_text(
                "command=import\npid=123\n", encoding="utf-8"
            )
            data = json.dumps({"file_id": 1}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target)
                body: dict[str, object] | None = None
                status = None

                def respond_json(
                    self, content: dict[str, object], *, status=None
                ) -> None:
                    self.body = content
                    self.status = status

            handler = FakeHandler()
            server_endpoints_items.respond_undelete_item(handler)  # type: ignore[arg-type]

            self.assertEqual(HTTPStatus.CONFLICT, handler.status)
            self.assertEqual(False, handler.body["ok"])
            self.assertIn("Bildesamlingen er låst", str(handler.body["error"]))
            self.assertTrue(deleted.exists())

    def test_run_server_item_page_can_rotate_image_view(self) -> None:
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
            image_path = target / "2024" / "01" / "IMG_20240102.png"
            original_bytes = image_path.read_bytes()
            data = json.dumps({"file_id": 1, "direction": "right"}).encode("utf-8")

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
            server_endpoints_items.respond_rotate_item(handler)  # type: ignore[arg-type]
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
            )
            month_body = month_page_html(
                target, "2024-01", browser_month_items(target, "2024-01")
            )
            final_bytes = image_path.read_bytes()

        self.assertEqual({"ok": True, "file_id": 1, "rotation": 90}, handler.body)
        self.assertEqual(original_bytes, final_bytes)
        self.assertIn("↺", body)
        self.assertIn("↻", body)
        self.assertIn('data-rotate-direction="left"', body)
        self.assertIn('data-rotate-direction="right"', body)
        self.assertIn('class="media-link quarter-turn"', body)
        self.assertIn("transform: rotate(90deg)", body)
        self.assertIn("--quarter-turn-width:", body)
        self.assertIn("transform: rotate(90deg)", month_body)
        self.assertIn(
            ".server-browser {\n      height: 100vh;\n      height: 100dvh;", SERVER_CSS
        )
        self.assertIn(".stage img, .stage video", SERVER_CSS)
        self.assertIn("touch-action: pan-y;", SERVER_CSS)
        self.assertIn("-webkit-user-drag: none;", SERVER_CSS)
        self.assertIn("width: auto;", SERVER_CSS)
        self.assertIn("height: auto;", SERVER_CSS)
        self.assertIn("object-fit: contain;", SERVER_CSS)
        self.assertIn("max-height: 100%;", SERVER_CSS)
        self.assertIn(".stage .media-link.quarter-turn", SERVER_CSS)
        self.assertIn("overflow: visible;", SERVER_CSS)
        self.assertIn('.stage img[data-view-rotation="90"]', SERVER_CSS)
        self.assertIn('.stage img[data-view-rotation="270"]', SERVER_CSS)
        self.assertIn(
            "max-width: min(calc(100vh - 10rem), var(--quarter-turn-width, 100%));",
            SERVER_CSS,
        )
        self.assertIn(
            "max-width: min(calc(100dvh - 10rem), var(--quarter-turn-width, 100%));",
            SERVER_CSS,
        )
        self.assertIn("max-height: none;", SERVER_CSS)
        self.assertIn(
            ".person-media {\n      position: relative;\n      display: grid;",
            SERVER_CSS,
        )
        self.assertIn(".person-media > a", SERVER_CSS)
        self.assertIn(
            "width: 100%;\n      height: 100%;\n      max-width: 100%;\n      max-height: 100%;",
            SERVER_CSS,
        )
        self.assertIn(".person-media img", SERVER_CSS)
        self.assertIn("max-width: var(--quarter-turn-width, 100%);", SERVER_CSS)
        self.assertNotIn(
            ".person-media {\n      position: relative;\n      display: inline-block;",
            SERVER_CSS,
        )
        self.assertNotIn("max-width: min(100%, 92vw);", SERVER_CSS)
        self.assertNotIn(
            "max-height: calc(100vh - 10rem);\n      transform-origin: center center;",
            SERVER_CSS,
        )
        self.assertNotIn(
            "max-height: calc(100vh - 10rem);\n      object-fit: contain;", SERVER_CSS
        )
        self.assertNotIn("padding: 14px;", SERVER_CSS)
        self.assertIn("function fitQuarterTurnMedia()", SERVER_JS)
        self.assertIn(
            "const availableHeight = Math.max(stageRect.height, 1);", SERVER_JS
        )
        self.assertIn(
            "const maxOriginalWidth = Math.max(Math.min(availableHeight, availableWidth * ratio), 1);",
            SERVER_JS,
        )
        rotate_handler = SERVER_JS[
            SERVER_JS.index(
                'document.querySelectorAll("[data-rotate-item]'
            ) : SERVER_JS.index('document.querySelectorAll("[data-tag-toggle]')
        ]
        self.assertIn("applyViewRotation(payload.rotation);", rotate_handler)
        self.assertIn(
            'const img = document.querySelector(".stage .media-link > img, .stage .person-media img");',
            SERVER_JS,
        )
        self.assertIn('link.classList.add("quarter-turn");', SERVER_JS)
        self.assertNotIn("window.location.reload();", rotate_handler)

    def test_run_server_rotate_reports_target_lock_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / LOCK_FILENAME).write_text("command=remove\n", encoding="utf-8")
            data = json.dumps({"file_id": 1, "direction": "right"}).encode("utf-8")

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(data)),
                    "Content-Type": "application/json",
                }
                rfile = BytesIO(data)
                server = SimpleNamespace(target=target)
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
            server_endpoints_items.respond_rotate_item(handler)  # type: ignore[arg-type]

        self.assertEqual(handler.status, HTTPStatus.CONFLICT)
        self.assertIn("Bildesamlingen er låst", str(handler.body["error"]))

    def test_run_server_rotate_image_view_wraps_left(self) -> None:
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
            data = json.dumps({"file_id": 1, "direction": "left"}).encode("utf-8")

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
            server_endpoints_items.respond_rotate_item(handler)  # type: ignore[arg-type]

        self.assertEqual({"ok": True, "file_id": 1, "rotation": 270}, handler.body)

    def test_run_server_rotate_redirects_when_current_item_leaves_filter(self) -> None:
        cases = (
            (3, 1, 90, "left", "/filter/is%3Arotated/item/2"),
            (3, 3, 270, "right", "/filter/is%3Arotated/item/2"),
            (1, 1, 90, "left", "/filter/is%3Arotated"),
        )
        for image_count, file_id, initial_rotation, direction, redirect_url in cases:
            with (
                self.subTest(
                    image_count=image_count,
                    file_id=file_id,
                    initial_rotation=initial_rotation,
                    direction=direction,
                ),
                tempfile.TemporaryDirectory() as tmp,
            ):
                target = Path(tmp) / "target"
                source = Path(tmp) / "source"
                source.mkdir()
                for day in range(1, image_count + 1):
                    (source / f"IMG_202401{day:02d}.png").write_bytes(
                        minimal_png(100 + day, 80)
                    )

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
                conn = db.connect(target)
                try:
                    conn.execute(
                        "UPDATE files SET view_rotation_degrees = 90 WHERE deleted_at IS NULL"
                    )
                    conn.execute(
                        "UPDATE files SET view_rotation_degrees = ? WHERE id = ?",
                        (initial_rotation, file_id),
                    )
                    conn.commit()
                finally:
                    conn.close()
                filter_source = text_filter_browser_source("is:rotated", target)
                data = json.dumps(
                    {
                        "file_id": file_id,
                        "direction": direction,
                        "source_url": filter_source.root_url,
                    }
                ).encode("utf-8")

                class FakeHandler:
                    headers = {
                        "Content-Length": str(len(data)),
                        "Content-Type": "application/json",
                    }
                    rfile = BytesIO(data)
                    server = SimpleNamespace(target=target)
                    body: dict[str, object] | None = None

                    def respond_json(
                        self, content: dict[str, object], *, status=None
                    ) -> None:
                        self.body = content

                handler = FakeHandler()
                server_endpoints_items.respond_rotate_item(handler)  # type: ignore[arg-type]

                self.assertEqual(0, handler.body["rotation"])
                self.assertEqual(redirect_url, handler.body["redirect_url"])

    def test_run_server_rotate_does_not_redirect_while_item_still_matches_filter(
        self,
    ) -> None:
        cases = (
            (180, "left", 90),
            (90, "right", 180),
            (180, "right", 270),
        )
        for initial_rotation, direction, expected_rotation in cases:
            with (
                self.subTest(initial_rotation=initial_rotation),
                tempfile.TemporaryDirectory() as tmp,
            ):
                target = Path(tmp) / "target"
                source = Path(tmp) / "source"
                source.mkdir()
                (source / "IMG_20240101.png").write_bytes(minimal_png(100, 80))

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
                conn = db.connect(target)
                try:
                    conn.execute(
                        "UPDATE files SET view_rotation_degrees = ? WHERE id = 1",
                        (initial_rotation,),
                    )
                    conn.commit()
                finally:
                    conn.close()
                filter_source = text_filter_browser_source("is:rotated", target)
                data = json.dumps(
                    {
                        "file_id": 1,
                        "direction": direction,
                        "source_url": filter_source.root_url,
                    }
                ).encode("utf-8")

                class FakeHandler:
                    headers = {
                        "Content-Length": str(len(data)),
                        "Content-Type": "application/json",
                    }
                    rfile = BytesIO(data)
                    server = SimpleNamespace(target=target)
                    body: dict[str, object] | None = None

                    def respond_json(
                        self, content: dict[str, object], *, status=None
                    ) -> None:
                        self.body = content

                handler = FakeHandler()
                server_endpoints_items.respond_rotate_item(handler)  # type: ignore[arg-type]

                self.assertEqual(expected_rotation, handler.body["rotation"])
                self.assertNotIn("redirect_url", handler.body)

    def test_run_server_rotate_image_view_rejects_invalid_direction(self) -> None:
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
            data = json.dumps({"file_id": 1, "direction": "up"}).encode("utf-8")

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
            server_endpoints_items.respond_rotate_item(handler)  # type: ignore[arg-type]

        self.assertEqual(
            {"ok": False, "error": "Ugyldig rotasjonsretning."}, handler.body
        )
        self.assertEqual(HTTPStatus.BAD_REQUEST, handler.status)

    def test_run_server_video_page_does_not_show_rotation_buttons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "video.mp4").write_bytes(
                minimal_mp4_with_creation_date(dt.date(2024, 1, 2))
            )

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
            body = item_page_html(
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
            )

        self.assertNotIn("↻", body)
        self.assertNotIn("↺", body)
