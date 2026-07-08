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
from bildebank.server_app import MaintenanceStatus
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

            statuses = (
                MaintenanceStatus("face-scan", 2, 1, 1, "/help/face-scan.md"),
                MaintenanceStatus("geo-scan", 2, 1, 1, "/help/geo-scan.md"),
                MaintenanceStatus("image-scan", 2, 2, 0, "/help/image-scan.md"),
            )
            with patch("bildebank.server_dashboard.server_app.maintenance_statuses", return_value=statuses):
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
        self.assertIn("bildebank refresh-metadata", body)
        self.assertIn("bildebank geo-scan", body)
        self.assertIn("bildebank face-scan", body)
        self.assertIn("Oppdatert", body)
        self.assertIn(r"bildebank backup --dry-run D:\Backuper", body)
        self.assertIn("/settings", body)
        self.assertIn("/sources", body)
        self.assertIn("/geo/stats", body)

    def test_run_server_dashboard_actions_show_updated_when_scans_are_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            insert_test_file(target, "2024/01/current.jpg", sha256="sha-current", gps_scanned=True)
            statuses = (
                MaintenanceStatus("face-scan", 1, 1, 0, "/help/face-scan.md"),
                MaintenanceStatus("geo-scan", 1, 1, 0, "/help/geo-scan.md"),
                MaintenanceStatus("image-scan", 1, 1, 0, "/help/image-scan.md"),
            )
            with patch("bildebank.server_dashboard.server_app.maintenance_statuses", return_value=statuses):
                summary = dashboard_summary(target, AppConfig())

        actions = dashboard_actions(summary)
        self.assertIn("oppdatert", [action.severity for action in actions])
        self.assertNotIn("bildebank geo-scan", [action.command for action in actions])
        self.assertNotIn("bildebank face-scan", [action.command for action in actions])
        self.assertNotIn("bildebank image-scan", [action.command for action in actions])

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

    def test_run_server_dashboard_uses_common_header_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            server = SimpleNamespace(target=target, config=AppConfig(), face_enabled=True, openclip_enabled=True)
            statuses = (
                MaintenanceStatus("face-scan", 0, 0, 0, "/help/face-scan.md"),
                MaintenanceStatus("geo-scan", 0, 0, 0, "/help/geo-scan.md"),
                MaintenanceStatus("image-scan", 0, 0, 0, "/help/image-scan.md"),
            )
            with patch("bildebank.server_dashboard.server_app.maintenance_statuses", return_value=statuses):
                body = routed_dashboard_page_html(server)

        self.assertIn('<header class="browser-header">', body)
        self.assertIn('href="/dashboard">Dashboard</a>', body)
        self.assertIn("Dashboard", body)

