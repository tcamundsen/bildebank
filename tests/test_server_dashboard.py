from __future__ import annotations

import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bildebank import db
from bildebank.config import AppConfig
from bildebank.db import init_database
from bildebank.server import BildebankRequestHandler
from bildebank.server_assets import SERVER_JS
from bildebank.server_dashboard import dashboard_actions, dashboard_page_html, dashboard_summary
from bildebank.server_pages import dashboard_page_html as routed_dashboard_page_html
from tests.db_test_helpers import insert_test_file


class ServerDashboardTests(unittest.TestCase):
    def test_run_server_dashboard_renders_collection_status_and_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            image_id = insert_test_file(target, "2024/01/image.jpg", sha256="sha-image", gps_scanned=True)
            video_id = insert_test_file(target, "2024/01/video.mp4", sha256="sha-video")
            insert_test_file(target, "deleted/2024/01/deleted.jpg", sha256="sha-deleted", deleted=True)

            conn = db.connect(target)
            try:
                imported_source = db.add_named_source(conn, Path(tmp) / "source-a", "source-a")
                db.mark_source_imported(conn, imported_source)
                error_source = db.add_named_source(conn, Path(tmp) / "source-b", "source-b")
                db.mark_source_error(conn, error_source)
                db.insert_or_validate_file_source(
                    conn,
                    file_id=image_id,
                    source_id=imported_source,
                    source_path=str(Path(tmp) / "source-a" / "image.jpg"),
                    source_path_key="source-a/image.jpg",
                    sha256="sha-image",
                    size_bytes=123,
                )
                db.insert_or_validate_file_source(
                    conn,
                    file_id=image_id,
                    source_id=error_source,
                    source_path=str(Path(tmp) / "source-b" / "image.jpg"),
                    source_path_key="source-b/image.jpg",
                    sha256="sha-image",
                    size_bytes=123,
                )
                conn.execute("UPDATE files SET taken_date = NULL, date_source = 'unknown' WHERE id = ?", (video_id,))
                conn.execute("UPDATE files SET name_conflict = 1 WHERE id = ?", (image_id,))
                db.insert_error(conn, source_id=error_source, source_path=Path("bad.jpg"), stage="import", message="feil")
                db.create_pending_file_move(
                    conn,
                    file_id=image_id,
                    target_root=target,
                    from_path=target / "2024/01/image.jpg",
                    to_path=target / "2024/01/image-renamed.jpg",
                    sha256="sha-image",
                    operation="refresh-metadata",
                )
                conn.commit()
            finally:
                conn.close()

            with patch("bildebank.server_app.maintenance_statuses", side_effect=AssertionError("maintenance count")):
                body = dashboard_page_html(
                    target,
                    AppConfig(),
                    shell_page_html=lambda title, content, **kwargs: content,
                )

        self.assertIn("Samlingsoversikt", body)
        self.assertIn("<dt>Aktive filer</dt>", body)
        self.assertIn("<dd>2</dd>", body)
        self.assertIn("<dt>Bilder</dt>", body)
        self.assertIn("<dt>Videoer</dt>", body)
        self.assertIn("<dt>Slettede bilder</dt>", body)
        self.assertIn("<dt>Kilder: error</dt>", body)
        self.assertIn("<dt>Kilder: imported</dt>", body)
        self.assertIn("<dt>Registrerte kildefiler</dt>", body)
        self.assertIn("<dt>Duplikatkilder</dt>", body)
        self.assertIn("Uløste feil", body)
        self.assertIn("bildebank errors", body)
        self.assertIn("bildebank doctor", body)
        self.assertIn("Navnekollisjoner", body)
        self.assertIn("Ufarlig: Bildebank er designet for å håndtere dette.", body)
        self.assertIn('href="/help/show-conflict.md"', body)
        self.assertIn("bildebank refresh-metadata", body)
        self.assertIn("bildebank geo-scan", body)
        self.assertIn("bildebank face-scan", body)
        self.assertIn("bildebank image-scan", body)
        self.assertIn('data-maintenance-name="geo-scan"', body)
        self.assertIn('data-maintenance-name="face-scan"', body)
        self.assertIn('data-maintenance-name="image-scan"', body)
        self.assertIn('data-maintenance-gui-label="Les GPS fra bilder"', body)
        self.assertIn('data-maintenance-gui-label="Finn ansikter"', body)
        self.assertIn('data-maintenance-gui-label="Klargjør bildesøk"', body)
        self.assertIn("Oppdaterer tall...", body)
        self.assertIn("Oppdaterer...", body)
        self.assertIn("data-maintenance-current", body)
        self.assertIn("data-maintenance-missing", body)
        self.assertIn("data-maintenance-total", body)
        self.assertIn("maintenanceGuiLabel", SERVER_JS)
        self.assertIn("dashboard-action-current", SERVER_JS)
        self.assertIn("I Bildebank-vinduet kan du trykke &quot;Lag miniatyrbilder&quot;.", body)
        self.assertIn(r"bildebank backup --dry-run D:\Backuper", body)
        self.assertIn("/settings", body)
        self.assertIn("/sources", body)
        self.assertIn("/geo/stats", body)

    def test_run_server_dashboard_actions_defer_scan_counts_to_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            insert_test_file(target, "2024/01/current.jpg", sha256="sha-current", gps_scanned=True)
            summary = dashboard_summary(target)

        actions = dashboard_actions(summary)
        scan_actions = {action.title: action for action in actions if action.maintenance_name}
        self.assertEqual(set(scan_actions), {"geo-scan", "face-scan", "image-scan"})
        self.assertEqual(scan_actions["geo-scan"].detail, "Oppdaterer tall...")
        self.assertEqual(scan_actions["geo-scan"].gui_label, "Les GPS fra bilder")
        self.assertEqual(scan_actions["face-scan"].gui_label, "Finn ansikter")
        self.assertEqual(scan_actions["image-scan"].gui_label, "Klargjør bildesøk")

    def test_run_server_dashboard_name_conflicts_are_info_not_recommended_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            file_id = insert_test_file(target, "2024/01/image.jpg", sha256="sha-image")
            conn = db.connect(target)
            try:
                conn.execute("UPDATE files SET name_conflict = 1 WHERE id = ?", (file_id,))
                conn.commit()
            finally:
                conn.close()
            with patch("bildebank.server_app.maintenance_statuses", side_effect=AssertionError("maintenance count")):
                summary = dashboard_summary(target)
                body = dashboard_page_html(
                    target,
                    AppConfig(),
                    shell_page_html=lambda title, content, **kwargs: content,
                )

        self.assertEqual(summary.name_conflicts, 1)
        self.assertNotIn("Navnekollisjoner", [action.title for action in dashboard_actions(summary)])
        self.assertIn("Navnekollisjoner", body)
        self.assertIn("Ufarlig: Bildebank er designet for å håndtere dette.", body)

    def test_run_server_dashboard_route_is_available_in_read_only_mode(self) -> None:
        handler = object.__new__(BildebankRequestHandler)
        handler.server = SimpleNamespace(read_only=True)
        handler.path = "/dashboard"
        handler.html_response = None
        handler.text_response = None
        handler.respond_html = lambda content, *, status=HTTPStatus.OK: setattr(
            handler, "html_response", (content, status)
        )
        handler.respond_text = lambda content, *, status=HTTPStatus.OK: setattr(
            handler, "text_response", (content, status)
        )

        with patch("bildebank.server.dashboard_page_html", return_value="<h1>Dashboard</h1>"):
            BildebankRequestHandler.do_GET(handler)  # type: ignore[arg-type]

        self.assertEqual(handler.html_response, ("<h1>Dashboard</h1>", HTTPStatus.OK))
        self.assertIsNone(handler.text_response)
        self.assertFalse(handler.read_only_get_blocked("/api/maintenance/statuses"))
        self.assertTrue(handler.read_only_get_blocked("/api/maintenance/thumbnails"))

    def test_run_server_dashboard_uses_common_header_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            server = SimpleNamespace(target=target, config=AppConfig(), face_enabled=True, openclip_enabled=True)
            with patch("bildebank.server_app.maintenance_statuses", side_effect=AssertionError("maintenance count")):
                body = routed_dashboard_page_html(server)

        self.assertIn('<header class="browser-header">', body)
        self.assertIn('href="/dashboard">Dashboard</a>', body)
        self.assertIn("Dashboard", body)
