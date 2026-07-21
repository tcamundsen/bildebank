from __future__ import annotations

import tempfile
import unittest
import uuid
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bildebank import db
from bildebank.config import AppConfig
from bildebank.db import init_database
from bildebank.geo import h3_cells_for_point
from bildebank.program_state import record_published_snapshot
from bildebank.server_handler import BildebankRequestHandler
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
            manual_id = insert_test_file(target, "2024/01/manual.jpg", sha256="sha-manual")
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
                conn.execute("UPDATE files SET gps_error = 'exiftool' WHERE id = ?", (video_id,))
                db.set_file_manual_h3_location(
                    conn,
                    file_id=manual_id,
                    h3_cells=h3_cells_for_point(59.91273, 10.74609),
                )
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
                    program_root=Path(tmp) / "program",
                )

        self.assertIn("Samlingsoversikt", body)
        self.assertIn("<dt>Aktive filer</dt>", body)
        self.assertIn("<dd>3</dd>", body)
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
        self.assertNotIn("bildebank geo-scan", body)
        self.assertNotIn("bildebank face-scan", body)
        self.assertNotIn("bildebank image-scan", body)
        self.assertIn('data-maintenance-name="geo-scan"', body)
        geo_scan_start = body.index('data-maintenance-name="geo-scan"')
        geo_scan_end = body.index("</article>", geo_scan_start)
        geo_scan_html = body[geo_scan_start:geo_scan_end]
        self.assertIn("<dt>Manuell H3</dt><dd>1</dd>", geo_scan_html)
        self.assertIn("<dt>Feil</dt><dd>1</dd>", geo_scan_html)
        self.assertIn('data-maintenance-name="face-scan"', body)
        self.assertIn('data-maintenance-name="image-scan"', body)
        self.assertIn('data-maintenance-gui-label="Les GPS fra bilder"', body)
        self.assertIn('data-maintenance-gui-label="Finn ansikter"', body)
        self.assertIn('data-maintenance-gui-label="Klargjør bildesøk"', body)
        self.assertIn("Oppdaterer tall...", body)
        self.assertIn("data-maintenance-current", body)
        self.assertIn("data-maintenance-missing", body)
        self.assertIn("data-maintenance-total", body)
        self.assertIn("data-maintenance-coverage-status", body)
        self.assertIn('<span data-maintenance-current>-</span> av <span data-maintenance-total>-</span>', body)
        self.assertIn("maintenanceGuiLabel", SERVER_JS)
        self.assertIn("${payload.missing} bilder trenger ${payload.name}. Kjør ", SERVER_JS)
        self.assertIn('eller klikk "${guiLabel}" på Verktøy-siden i Bildebank-vinduet.', SERVER_JS)
        self.assertIn("dashboard-action-current", SERVER_JS)
        self.assertIn("`${payload.current} av ${payload.total}`", SERVER_JS)
        self.assertIn("I Bildebank-vinduet kan du trykke &quot;Lag miniatyrbilder&quot;.", body)
        actions_start = body.index('<section class="dashboard-section"')
        actions_end = body.index('<section class="dashboard-grid"', actions_start)
        actions_html = body[actions_start:actions_end]
        thumbnail_start = actions_html.index("data-thumbnail-maintenance")
        thumbnail_end = actions_html.index("</article>", thumbnail_start)
        thumbnail_html = actions_html[thumbnail_start:thumbnail_end]
        self.assertIn("Thumbnails", thumbnail_html)
        self.assertNotIn("bildebank make-thumbnails", thumbnail_html)
        self.assertIn("data-thumbnail-status", thumbnail_html)
        self.assertIn("Tell thumbnails", thumbnail_html)
        self.assertIn("data-count-thumbnails", thumbnail_html)
        self.assertIn("data-thumbnail-current", thumbnail_html)
        self.assertIn("data-thumbnail-missing", thumbnail_html)
        self.assertIn("data-thumbnail-total", thumbnail_html)
        self.assertLess(thumbnail_html.index("Ikke telt ennå"), thumbnail_html.index("Tell thumbnails"))
        self.assertLess(thumbnail_html.index("Tell thumbnails"), thumbnail_html.index("data-thumbnail-current"))
        coverage_start = body.index("<h2>Dekning</h2>")
        coverage_end = body.index("</section>", coverage_start)
        coverage_html = body[coverage_start:coverage_end]
        self.assertIn("Thumbnails", coverage_html)
        self.assertIn("Telles under Anbefalte handlinger", coverage_html)
        self.assertIn("data-thumbnail-coverage-status", coverage_html)
        self.assertNotIn("data-count-thumbnails", coverage_html)
        self.assertNotIn("data-thumbnail-current", coverage_html)
        self.assertIn("Snapshots", body)
        self.assertIn("Ingen publiserte snapshots er registrert", body)
        self.assertNotIn('href="/help/backup.md"', body)
        self.assertIn('href="/help/snapshot.md"', body)
        self.assertIn("/settings", body)
        self.assertIn("/sources", body)
        self.assertNotIn("/geo/stats", body)

    def test_dashboard_distinguishes_repositories_with_the_same_last_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            program_root = root / "program"
            repository = root / "usb-f" / "Familiebilder"
            program_root.mkdir()
            repository.mkdir(parents=True)
            init_database(target)
            conn = db.connect(target)
            try:
                collection_id = db.validate_collection_id(conn)
            finally:
                conn.close()
            repository_ids = [str(uuid.uuid4()) for _index in range(3)]
            statuses = ("complete", "degraded", "recovery")
            for index, (repository_id, status) in enumerate(
                zip(repository_ids, statuses, strict=True),
                start=1,
            ):
                record_published_snapshot(
                    program_root,
                    collection_id=collection_id,
                    repository_id=repository_id,
                    repository_path=repository,
                    snapshot_id=str(uuid.uuid4()),
                    status=status,
                    published_at=f"2026-07-{index:02d}T12:00:00Z",
                )

            body = dashboard_page_html(
                target,
                AppConfig(),
                shell_page_html=lambda title, content, **kwargs: content,
                program_root=program_root,
            )

        for repository_id in repository_ids:
            self.assertIn(repository_id[:8], body)
        self.assertEqual(body.count(str(repository.resolve())), 3)
        self.assertIn("complete – uten kjente avvik", body)
        self.assertIn("degraded – publisert med problemer", body)
        self.assertIn("recovery – kan ikke brukes som vanlig hel restore", body)

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
        self.assertEqual(scan_actions["geo-scan"].fact_rows, (("Manuell H3", "0"), ("Feil", "0")))
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

        with patch("bildebank.server_handler.dashboard_page_html", return_value="<h1>Dashboard</h1>"):
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
