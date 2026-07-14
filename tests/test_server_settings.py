from __future__ import annotations

import os
import re
import tempfile
import unittest
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bildebank.config import (
    AppConfig,
    BrowserConfig,
    BrowserHotkeyConfig,
    FaceRecognitionConfig,
    OpenClipConfig,
    load_config,
)
from bildebank.db import init_database
from bildebank.geo import h3_cells_for_point
from bildebank.openclip import connect_openclip_db, embedding_blob
from bildebank import server_endpoints_admin
from bildebank.server_handler import BildebankRequestHandler
from bildebank.server_app import (
    MaintenanceStatus,
    maintenance_statuses,
    thumbnail_maintenance_status,
)
from bildebank.server_assets import SERVER_ASSET_VERSION, SERVER_CSS, SERVER_JS
from bildebank.server_pages import app_status_page_html
from bildebank.server_search import OpenClipSearchCache
from bildebank.thumbnails import thumbnail_absolute_path
from tests.db_test_helpers import insert_test_file


class ServerSettingsTests(unittest.TestCase):
    def test_run_server_app_status_page_shows_config_and_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "program"
            target = Path(tmp) / "target"
            model_root = root / ".bildebank-insightface"
            (model_root / "models" / "antelopev2").mkdir(parents=True)
            (model_root / "models" / "antelopev2" / "scrfd_10g_bnkps.onnx").write_bytes(
                b"model"
            )
            (model_root / "models" / "buffalo_l").mkdir(parents=True)
            (model_root / "models" / "buffalo_l" / "det_10g.onnx").write_bytes(b"model")
            config = AppConfig(
                face_recognition=FaceRecognitionConfig(
                    enabled=True, model_root=model_root, model_name="buffalo_l"
                ),
                openclip=OpenClipConfig(
                    enabled=True,
                    model_name="Test-Model",
                    pretrained="test-weights",
                    device="cpu",
                ),
                browser=BrowserConfig(
                    hide_out_of_focus=True,
                    manual_person_controls_enabled=False,
                    hotkey_hints_enabled=True,
                    hotkeys={
                        "1": BrowserHotkeyConfig(action="person", person_name="Kari")
                    },
                ),
            )

            with (
                patch(
                    "bildebank.server_app.module_available",
                    side_effect=lambda name: name == "open_clip",
                ),
                patch(
                    "bildebank.server_app.maintenance_statuses",
                    side_effect=AssertionError("maintenance count"),
                ),
                patch(
                    "bildebank.server_app.active_thumbnail_candidates",
                    side_effect=AssertionError("thumbnail count"),
                ),
            ):
                body = app_status_page_html(target, config, scroll_y=312)

        self.assertIn("<h2>Innstillinger</h2>", body)
        self.assertIn('data-settings-scroll-restore="312"', body)
        self.assertIn("function setSettingsScrollField", SERVER_JS)
        self.assertIn('input[name="scroll_y"]', SERVER_JS)
        self.assertIn("window.scrollTo", SERVER_JS)
        self.assertIn("Bildebank-versjon", body)
        self.assertIn("Bildesamling", body)
        self.assertNotIn("Vedlikehold", body)
        self.assertNotIn('href="/help/face-scan.md"', body)
        self.assertNotIn('href="/help/geo-scan.md"', body)
        self.assertNotIn('href="/help/image-scan.md"', body)
        self.assertNotIn('href="/help/make-thumbnails.md"', body)
        self.assertNotIn("face-scan", body)
        self.assertNotIn("geo-scan", body)
        self.assertNotIn("image-scan", body)
        self.assertNotIn("Oppdaterer...", body)
        self.assertNotIn("data-maintenance-name", body)
        self.assertNotIn("data-thumbnail-maintenance", body)
        self.assertNotIn("thumbnails", body)
        self.assertNotIn("Ikke telt ennå", body)
        self.assertNotIn("Tell thumbnails", body)
        self.assertNotIn("data-count-thumbnails", body)
        self.assertNotIn("data-thumbnail-current", body)
        self.assertNotIn("data-thumbnail-missing", body)
        self.assertNotIn("data-thumbnail-total", body)
        self.assertIn("/api/maintenance/statuses", SERVER_JS)
        self.assertIn("data-maintenance-name", SERVER_JS)
        self.assertIn(
            'window.addEventListener("load", scheduleMaintenanceStatusesLoad', SERVER_JS
        )
        self.assertIn("setTimeout(loadMaintenanceStatuses, 0)", SERVER_JS)
        self.assertEqual(SERVER_ASSET_VERSION, "51")
        self.assertIn("bildebank ${payload.name}", SERVER_JS)
        self.assertIn("bilder trenger ${payload.name}", SERVER_JS)
        self.assertIn("/api/maintenance/thumbnails", SERVER_JS)
        self.assertIn("Teller thumbnails...", SERVER_JS)
        self.assertIn("data-thumbnail-coverage-status", SERVER_JS)
        self.assertIn('Klikk "Lag miniatyrbilder" i Bildebank-vinduet.', SERVER_JS)
        self.assertNotIn("bilder mangler thumbnails, kjør", SERVER_JS)
        self.assertIn(".maintenance-row", SERVER_CSS)
        self.assertIn("grid-template-columns: minmax(110px, 150px)", SERVER_CSS)
        settings_start = body.index('<dl class="info-list app-status">')
        settings_html = body[settings_start : body.index("</dl>", settings_start)]
        settings_labels = re.findall(r"<dt>(.*?)</dt>", settings_html)
        self.assertEqual(
            settings_labels,
            [
                "Bildesamling",
                "Bildebank-versjon",
                "Skjul bilder tagget “Ute av fokus”",
                "Hurtigtaster 1-5",
                "InsightFace aktivert",
                "InsightFace-modell",
                "InsightFace installert",
                "GUI for manuell bekrefting av person i bildet",
                "Referanselenke for ansiktsforslag",
                "OpenCLIP tilgjengelig",
                "Bildesøk aktivert",
                "OpenCLIP-modell",
                "OpenCLIP-pretrained",
                "OpenCLIP-device",
            ],
        )
        self.assertEqual(
            settings_labels[4:8],
            [
                "InsightFace aktivert",
                "InsightFace-modell",
                "InsightFace installert",
                "GUI for manuell bekrefting av person i bildet",
            ],
        )
        self.assertIn('action="/settings/hide-out-of-focus"', body)
        self.assertNotIn('action="/settings/manual-h3-cell"', body)
        self.assertNotIn("Aktiv manuell H3-celle", body)
        self.assertIn("Hurtigtaster 1-5", body)
        self.assertIn('action="/settings/hotkey"', body)
        self.assertIn('action="/settings/hotkey-hints"', body)
        self.assertIn('href="/settings/h3-cells"', body)
        self.assertIn("Rediger H3-celler", body)
        self.assertIn("Aktiver hurtigtaster 1-5: På", body)
        self.assertIn('<input type="hidden" name="key" value="1">', body)
        self.assertLess(
            body.index('action="/settings/hotkey-hints"'),
            body.index('<input type="hidden" name="key" value="1">'),
        )
        self.assertIn("data-hotkey-action", body)
        self.assertIn('data-hotkey-fields="h3"', body)
        self.assertIn('data-hotkey-fields="manual_date"', body)
        self.assertIn('data-hotkey-fields="person"', body)
        self.assertIn('data-hotkey-fields="tag"', body)
        self.assertIn('data-hotkey-fields=""', body)
        self.assertIn(".hotkey-empty-fields", SERVER_CSS)
        self.assertIn("function updateHotkeyForm", SERVER_JS)
        self.assertIn('<option value="person" selected>Legg til person</option>', body)
        self.assertIn('<option value="tag">Sett tagg</option>', body)
        self.assertIn('<option value="Kari" selected>Kari</option>', body)
        self.assertIn('<span class="app-toggle-status">På</span>', body)
        self.assertIn(str(target), body)
        self.assertIn("InsightFace aktivert", body)
        self.assertIn('action="/settings/face-config"', body)
        self.assertIn('name="enabled" value="true" checked', body)
        self.assertIn("InsightFace-modell", body)
        self.assertIn('action="/settings/face-model"', body)
        self.assertIn("GUI for manuell bekrefting av person", body)
        self.assertIn('action="/settings/manual-person-controls"', body)
        self.assertIn("Referanselenke for ansiktsforslag", body)
        self.assertIn('action="/settings/person-reference-links"', body)
        self.assertIn('<span class="app-toggle-status">Av</span>', body)

        self.assertIn('<option value="antelopev2">antelopev2</option>', body)
        self.assertIn('<option value="buffalo_l" selected>buffalo_l</option>', body)
        self.assertIn("må installeres for å scanne ansikter i nye bilder.", body)
        self.assertNotIn("app-toggle-submit", body)
        self.assertIn("<dd>ja</dd>", body)
        self.assertIn("InsightFace installert", body)
        self.assertNotIn('href="/date-source/filename">Dato fra filnavn</a>', body)
        self.assertNotIn('href="/date-source/mtime">Dato fra mtime</a>', body)
        self.assertIn("OpenCLIP tilgjengelig", body)
        self.assertIn("Bildesøk aktivert", body)
        self.assertIn('action="/settings/image-search"', body)
        self.assertIn("Test-Model", body)
        self.assertIn("test-weights", body)
        self.assertIn("cpu", body)

    def test_run_server_settings_maintenance_status_counts_scan_needs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            insert_test_file(
                target, "2024/01/current.png", sha256="sha-current", gps_scanned=True
            )
            insert_test_file(target, "2024/01/missing.png", sha256="sha-missing")
            insert_test_file(
                target, "2024/01/deleted.png", sha256="sha-deleted", deleted=True
            )
            config = AppConfig(
                openclip=OpenClipConfig(
                    model_name="Test-Model", pretrained="test-weights"
                )
            )

            statuses = {
                status.name: status for status in maintenance_statuses(target, config)
            }

        self.assertEqual(statuses["face-scan"].total, 2)
        self.assertEqual(statuses["face-scan"].scanned, 0)
        self.assertEqual(statuses["face-scan"].missing, 2)
        self.assertEqual(statuses["geo-scan"].total, 2)
        self.assertEqual(statuses["geo-scan"].scanned, 1)
        self.assertEqual(statuses["geo-scan"].missing, 1)
        self.assertEqual(statuses["image-scan"].total, 2)
        self.assertEqual(statuses["image-scan"].scanned, 0)
        self.assertEqual(statuses["image-scan"].missing, 2)

    def test_run_server_maintenance_statuses_api_counts_scan_needs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            insert_test_file(
                target, "2024/01/current.png", sha256="sha-current", gps_scanned=True
            )
            insert_test_file(target, "2024/01/missing.png", sha256="sha-missing")
            insert_test_file(
                target, "2024/01/deleted.png", sha256="sha-deleted", deleted=True
            )
            config = AppConfig(
                openclip=OpenClipConfig(
                    model_name="Test-Model", pretrained="test-weights"
                )
            )
            response: dict[str, object] = {}
            handler = object.__new__(BildebankRequestHandler)
            handler.server = SimpleNamespace(target=target, config=config)

            def fake_respond_json(
                content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK
            ) -> None:
                response["content"] = content
                response["status"] = status

            handler.respond_json = fake_respond_json  # type: ignore[method-assign]
            handler.respond_maintenance_statuses()

        self.assertEqual(response["status"], HTTPStatus.OK)
        self.assertEqual(
            response["content"],
            {
                "ok": True,
                "statuses": [
                    {
                        "name": "face-scan",
                        "total": 2,
                        "current": 0,
                        "missing": 2,
                        "help_path": "/help/face-scan.md",
                    },
                    {
                        "name": "geo-scan",
                        "total": 2,
                        "current": 1,
                        "missing": 1,
                        "help_path": "/help/geo-scan.md",
                    },
                    {
                        "name": "image-scan",
                        "total": 2,
                        "current": 0,
                        "missing": 2,
                        "help_path": "/help/image-scan.md",
                    },
                ],
            },
        )

    def test_run_server_thumbnail_maintenance_api_counts_missing_current_and_active_images(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            insert_test_file(target, "2024/01/current.jpg", sha256="sha-current")
            insert_test_file(target, "2024/01/missing.png", sha256="sha-missing")
            insert_test_file(
                target, "2024/01/deleted.jpg", sha256="sha-deleted", deleted=True
            )
            insert_test_file(target, "2024/01/not-image.txt", sha256="sha-text")
            current_original = target / "2024/01/current.jpg"
            thumb_path = thumbnail_absolute_path(target, Path("2024/01/current.jpg"))
            thumb_path.parent.mkdir(parents=True)
            thumb_path.write_bytes(b"thumb")
            os.utime(
                thumb_path,
                ns=(
                    current_original.stat().st_mtime_ns,
                    current_original.stat().st_mtime_ns,
                ),
            )

            status = thumbnail_maintenance_status(target)
            response: dict[str, object] = {}
            handler = object.__new__(BildebankRequestHandler)
            handler.server = SimpleNamespace(target=target)

            def fake_respond_json(
                content: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK
            ) -> None:
                response["content"] = content
                response["status"] = status

            handler.respond_json = fake_respond_json  # type: ignore[method-assign]
            handler.respond_thumbnail_maintenance()

        self.assertEqual(status.total, 2)
        self.assertEqual(status.scanned, 1)
        self.assertEqual(status.missing, 1)
        self.assertEqual(response["status"], HTTPStatus.OK)
        self.assertEqual(
            response["content"],
            {"ok": True, "name": "thumbnails", "total": 2, "current": 1, "missing": 1},
        )

    def test_run_server_settings_image_scan_status_counts_current_embeddings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            current_id = insert_test_file(
                target, "2024/01/current.png", sha256="sha-current"
            )
            stale_id = insert_test_file(target, "2024/01/stale.png", sha256="sha-stale")
            config = AppConfig(
                openclip=OpenClipConfig(
                    model_name="Test-Model", pretrained="test-weights"
                )
            )
            conn = connect_openclip_db(target)
            try:
                conn.executemany(
                    """
                    INSERT INTO image_embeddings(
                        file_id, target_path, target_path_key, sha256, model_name, pretrained, embedding
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            current_id,
                            "2024/01/current.png",
                            "2024/01/current.png",
                            "sha-current",
                            "Test-Model",
                            "test-weights",
                            embedding_blob([1.0, 0.0]),
                        ),
                        (
                            stale_id,
                            "2024/01/stale.png",
                            "2024/01/stale.png",
                            "old-sha",
                            "Test-Model",
                            "test-weights",
                            embedding_blob([0.0, 1.0]),
                        ),
                    ],
                )
                conn.commit()
            finally:
                conn.close()

            statuses = {
                status.name: status for status in maintenance_statuses(target, config)
            }

        self.assertEqual(statuses["image-scan"].total, 2)
        self.assertEqual(statuses["image-scan"].scanned, 1)
        self.assertEqual(statuses["image-scan"].missing, 1)

    def test_run_server_settings_maintenance_status_shows_updated_when_current(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            file_id = insert_test_file(
                target, "2024/01/current.png", sha256="sha-current", gps_scanned=True
            )
            config = AppConfig(
                openclip=OpenClipConfig(
                    model_name="Test-Model", pretrained="test-weights"
                )
            )
            conn = connect_openclip_db(target)
            try:
                conn.execute(
                    """
                    INSERT INTO image_embeddings(
                        file_id, target_path, target_path_key, sha256, model_name, pretrained, embedding
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_id,
                        "2024/01/current.png",
                        "2024/01/current.png",
                        "sha-current",
                        "Test-Model",
                        "test-weights",
                        embedding_blob([1.0, 0.0]),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

            with patch(
                "bildebank.server_app.face_scan_maintenance_status"
            ) as face_status:
                face_status.return_value = MaintenanceStatus(
                    "face-scan", 1, 1, 0, "/help/face-scan.md"
                )
                statuses = {
                    status.name: status
                    for status in maintenance_statuses(target, config)
                }

        self.assertEqual(statuses["geo-scan"].missing, 0)
        self.assertEqual(statuses["image-scan"].missing, 0)

    def test_run_server_settings_hotkey_tag_select_keeps_missing_selected_tag(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            target.mkdir()
            body = app_status_page_html(
                target,
                AppConfig(
                    browser=BrowserConfig(
                        hotkeys={
                            "1": BrowserHotkeyConfig(action="tag", tag_name="Familie")
                        }
                    )
                ),
            )

        self.assertIn('<option value="tag" selected>Sett tagg</option>', body)
        self.assertIn('<option value="Familie" selected>Familie</option>', body)

    def test_run_server_face_config_post_updates_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"enabled=false&enabled=true"

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    config=AppConfig(
                        face_recognition=FaceRecognitionConfig(enabled=False)
                    )
                )
                location: str | None = None

                def redirect(self, location: str) -> None:
                    self.location = location

            handler = FakeHandler()
            with patch(
                "bildebank.server_app.server_program_repo_root", return_value=root
            ):
                server_endpoints_admin.respond_set_face_config(handler)  # type: ignore[arg-type]

            config = load_config(root)

        self.assertTrue(config.face_recognition.enabled)
        self.assertTrue(handler.server.config.face_recognition.enabled)
        self.assertEqual(handler.location, "/settings")

    def test_run_server_settings_post_redirect_preserves_scroll_position(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"enabled=false&enabled=true&scroll_y=312"

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    config=AppConfig(
                        face_recognition=FaceRecognitionConfig(enabled=False)
                    )
                )
                location: str | None = None

                def redirect(self, location: str) -> None:
                    self.location = location

            handler = FakeHandler()
            with patch(
                "bildebank.server_app.server_program_repo_root", return_value=root
            ):
                server_endpoints_admin.respond_set_face_config(handler)  # type: ignore[arg-type]

        self.assertEqual(handler.location, "/settings?scroll=312")

    def test_run_server_image_search_post_updates_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"enabled=false&enabled=true"

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    config=AppConfig(openclip=OpenClipConfig(enabled=False))
                )
                location: str | None = None

                def redirect(self, location: str) -> None:
                    self.location = location

            handler = FakeHandler()
            with patch(
                "bildebank.server_app.server_program_repo_root", return_value=root
            ):
                server_endpoints_admin.respond_set_image_search(handler)  # type: ignore[arg-type]

            config = load_config(root)
            config_text = (root / "bildebank-config.toml").read_text(encoding="utf-8")

        self.assertTrue(config.openclip.enabled)
        self.assertTrue(handler.server.config.openclip.enabled)
        self.assertIsInstance(handler.server.search_cache, OpenClipSearchCache)
        self.assertEqual(handler.location, "/settings")
        self.assertIn("[image_search]", config_text)
        self.assertIn("enabled = true", config_text)
        self.assertNotIn("[openclip]", config_text)

    def test_run_server_hide_out_of_focus_post_updates_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"enabled=false&enabled=true"

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(config=AppConfig())
                location: str | None = None

                def redirect(self, location: str) -> None:
                    self.location = location

            handler = FakeHandler()
            with patch(
                "bildebank.server_app.server_program_repo_root", return_value=root
            ):
                server_endpoints_admin.respond_set_hide_out_of_focus(handler)  # type: ignore[arg-type]

            config = load_config(root)

        self.assertTrue(config.browser.hide_out_of_focus)
        self.assertTrue(handler.server.config.browser.hide_out_of_focus)
        self.assertEqual(handler.location, "/settings")

    def test_run_server_manual_person_controls_post_updates_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"enabled=false"

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(
                    config=AppConfig(
                        browser=BrowserConfig(manual_person_controls_enabled=True)
                    )
                )
                location: str | None = None

                def redirect(self, location: str) -> None:
                    self.location = location

            handler = FakeHandler()
            with patch(
                "bildebank.server_app.server_program_repo_root", return_value=root
            ):
                server_endpoints_admin.respond_set_manual_person_controls(handler)  # type: ignore[arg-type]

            config = load_config(root)

        self.assertFalse(config.browser.manual_person_controls_enabled)
        self.assertFalse(handler.server.config.browser.manual_person_controls_enabled)
        self.assertEqual(handler.location, "/settings")

    def test_run_server_person_reference_links_post_updates_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"enabled=false&enabled=true"

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(config=AppConfig())
                location: str | None = None

                def redirect(self, location: str) -> None:
                    self.location = location

            handler = FakeHandler()
            with patch(
                "bildebank.server_app.server_program_repo_root", return_value=root
            ):
                server_endpoints_admin.respond_set_person_reference_links(handler)  # type: ignore[arg-type]

            config = load_config(root)

        self.assertTrue(config.browser.person_reference_links_enabled)
        self.assertTrue(handler.server.config.browser.person_reference_links_enabled)
        self.assertEqual(handler.location, "/settings")

    def test_run_server_hotkey_hints_post_updates_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"enabled=true"

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(config=AppConfig())
                location: str | None = None

                def redirect(self, location: str) -> None:
                    self.location = location

            handler = FakeHandler()
            with patch(
                "bildebank.server_app.server_program_repo_root", return_value=root
            ):
                server_endpoints_admin.respond_set_hotkey_hints(handler)  # type: ignore[arg-type]

            config = load_config(root)

        self.assertTrue(config.browser.hotkey_hints_enabled)
        self.assertTrue(handler.server.config.browser.hotkey_hints_enabled)
        self.assertEqual(handler.location, "/settings")

    def test_run_server_hotkey_post_updates_config(self) -> None:
        h3_cell = h3_cells_for_point(59.91273, 10.74609)["h3_res7"]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = (
                f"key=1&action=h3&h3_cell={h3_cell}&person_name=&mode=exact&"
                "date=&uncertainty=1m&date_from=&date_to=&note="
            ).encode("utf-8")

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(config=AppConfig())
                location: str | None = None

                def redirect(self, location: str) -> None:
                    self.location = location

                def respond_text(self, content: str, *, status: HTTPStatus) -> None:
                    raise AssertionError(f"{status}: {content}")

            handler = FakeHandler()
            with patch(
                "bildebank.server_app.server_program_repo_root", return_value=root
            ):
                server_endpoints_admin.respond_set_hotkey(handler)  # type: ignore[arg-type]

            config = load_config(root)

        self.assertEqual(
            config.browser.hotkeys["1"],
            BrowserHotkeyConfig(action="h3", h3_cell=h3_cell),
        )
        self.assertEqual(
            handler.server.config.browser.hotkeys["1"],
            BrowserHotkeyConfig(action="h3", h3_cell=h3_cell),
        )
        self.assertEqual(handler.location, "/settings")

    def test_run_server_hotkey_post_updates_tag_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"key=1&action=tag&tag_name=+Familie++&h3_cell=&person_name=&mode=exact&date=&uncertainty=1m"

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(config=AppConfig())
                location: str | None = None

                def redirect(self, location: str) -> None:
                    self.location = location

                def respond_text(self, content: str, *, status: HTTPStatus) -> None:
                    raise AssertionError(f"{status}: {content}")

            handler = FakeHandler()
            with patch(
                "bildebank.server_app.server_program_repo_root", return_value=root
            ):
                server_endpoints_admin.respond_set_hotkey(handler)  # type: ignore[arg-type]

            config = load_config(root)
            config_text = (root / "bildebank-config.toml").read_text(encoding="utf-8")

        self.assertEqual(
            config.browser.hotkeys["1"],
            BrowserHotkeyConfig(action="tag", tag_name="Familie"),
        )
        self.assertEqual(
            handler.server.config.browser.hotkeys["1"],
            BrowserHotkeyConfig(action="tag", tag_name="Familie"),
        )
        self.assertIn('"1" = { action = "tag", tag_name = "Familie" }', config_text)
        self.assertEqual(handler.location, "/settings")

    def test_run_server_hotkey_post_rejects_invalid_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = (
                "key=5&action=manual_date&h3_cell=&person_name=&mode=between&"
                "date=&uncertainty=1m&date_from=2004-08-31&date_to=2004-06-01&note="
            ).encode("utf-8")
            response: dict[str, object] = {}

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(config=AppConfig())

                def respond_text(self, content: str, *, status: HTTPStatus) -> None:
                    response["content"] = content
                    response["status"] = status

            handler = FakeHandler()
            with patch(
                "bildebank.server_app.server_program_repo_root", return_value=root
            ):
                server_endpoints_admin.respond_set_hotkey(handler)  # type: ignore[arg-type]

        self.assertEqual(response["status"], HTTPStatus.BAD_REQUEST)
        self.assertIn("Fra-dato kan ikke være etter til-dato", str(response["content"]))

    def test_run_server_hotkey_post_rejects_empty_tag_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"key=1&action=tag&tag_name=+&h3_cell=&person_name=&mode=exact&date=&uncertainty=1m"
            response: dict[str, object] = {}

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(config=AppConfig())

                def respond_text(self, content: str, *, status: HTTPStatus) -> None:
                    response["content"] = content
                    response["status"] = status

            handler = FakeHandler()
            with patch(
                "bildebank.server_app.server_program_repo_root", return_value=root
            ):
                server_endpoints_admin.respond_set_hotkey(handler)  # type: ignore[arg-type]

        self.assertEqual(response["status"], HTTPStatus.BAD_REQUEST)
        self.assertIn("Taggnavn kan ikke være tomt", str(response["content"]))

    def test_run_server_face_model_post_updates_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_root = root / ".bildebank-insightface"
            (model_root / "models" / "antelopev2").mkdir(parents=True)
            (model_root / "models" / "antelopev2" / "scrfd_10g_bnkps.onnx").write_bytes(
                b"model"
            )
            (root / "bildebank-config.toml").write_text(
                """
[face_recognition]
model_root = ".bildebank-insightface"
model_name = "buffalo_l"
""",
                encoding="utf-8",
            )
            data = b"model_name=antelopev2"

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(config=load_config(root))
                location: str | None = None

                def redirect(self, location: str) -> None:
                    self.location = location

            handler = FakeHandler()
            with patch(
                "bildebank.server_app.server_program_repo_root", return_value=root
            ):
                server_endpoints_admin.respond_set_face_model(handler)  # type: ignore[arg-type]

            config = load_config(root)

        self.assertEqual(config.face_recognition.model_name, "antelopev2")
        self.assertEqual(
            handler.server.config.face_recognition.model_name, "antelopev2"
        )
        self.assertEqual(handler.location, "/settings")

    def test_run_server_face_model_post_rejects_not_installed_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bildebank-config.toml").write_text(
                """
[face_recognition]
model_root = ".bildebank-insightface"
model_name = "buffalo_l"
""",
                encoding="utf-8",
            )
            data = b"model_name=antelopev2"

            class FakeHandler:
                headers = {"Content-Length": str(len(data))}
                rfile = BytesIO(data)
                server = SimpleNamespace(config=load_config(root))

            handler = FakeHandler()
            with patch(
                "bildebank.server_app.server_program_repo_root", return_value=root
            ):
                with self.assertRaisesRegex(ValueError, "ikke installert"):
                    server_endpoints_admin.respond_set_face_model(handler)  # type: ignore[arg-type]

            config = load_config(root)

        self.assertEqual(config.face_recognition.model_name, "buffalo_l")
