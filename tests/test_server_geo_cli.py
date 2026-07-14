from __future__ import annotations

import tempfile
import unittest
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from bildebank import db, server_endpoints_admin
from bildebank.db import init_database
from bildebank.geo import h3_cells_for_manual_cell, h3_cells_for_point
from bildebank.server_assets import SERVER_JS
from bildebank.server_browser_info_html import image_info_content_html
from bildebank.server_browser_queries import (
    adjacent_browser_items,
    browser_item_by_id,
    browser_month_navigation,
)
from bildebank.server_pages import (
    geo_area_page_html,
    geo_index_page_html,
    geo_map_page_html,
    h3_cells_page_html,
    item_page_html,
)
from tests.cli_helpers import run_cli
from tests.db_test_helpers import register_target_file
from tests.test_media import minimal_png


class ServerGeoCliTests(unittest.TestCase):
    def test_run_server_h3_cells_page_saves_and_lists_named_cell(self) -> None:
        h3_cell = h3_cells_for_point(59.91273, 10.74609)["h3_res7"]
        form = f"name=Oslo&h3_cell={h3_cell}".encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            active_path = target / "2024/01/active.png"
            deleted_path = target / "2024/01/deleted.png"
            active_path.parent.mkdir(parents=True)
            active_path.write_bytes(minimal_png(100, 80))
            deleted_path.write_bytes(minimal_png(101, 80))
            active_id = register_target_file(target, Path("2024/01/active.png"))
            deleted_id = register_target_file(target, Path("2024/01/deleted.png"))
            cells = h3_cells_for_point(59.91273, 10.74609)
            conn = db.connect(target)
            try:
                db.update_file_gps(
                    conn,
                    file_id=active_id,
                    gps_lat=59.91273,
                    gps_lon=10.74609,
                    gps_alt=None,
                    h3_cells=cells,
                    gps_source="test",
                    gps_error=None,
                )
                db.update_file_gps(
                    conn,
                    file_id=deleted_id,
                    gps_lat=59.91273,
                    gps_lon=10.74609,
                    gps_alt=None,
                    h3_cells=cells,
                    gps_source="test",
                    gps_error=None,
                )
                conn.execute(
                    "UPDATE files SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (deleted_id,),
                )
                conn.commit()
            finally:
                conn.close()

            class FakeHandler:
                headers = {
                    "Content-Length": str(len(form)),
                    "Content-Type": "application/x-www-form-urlencoded",
                }
                rfile = BytesIO(form)
                server = SimpleNamespace(
                    target=target, face_enabled=True, openclip_enabled=True
                )
                redirect_url = ""

                def redirect(self, url: str) -> None:
                    self.redirect_url = url

                def respond_html(
                    self, content: str, *, status: HTTPStatus = HTTPStatus.OK
                ) -> None:
                    raise AssertionError(
                        f"Unexpected error response {status}: {content}"
                    )

            handler = FakeHandler()
            server_endpoints_admin.respond_set_h3_cell_name(handler)  # type: ignore[arg-type]
            body = h3_cells_page_html(target)

            conn = db.connect(target)
            try:
                rows = db.geo_place_names(conn)
            finally:
                conn.close()

        self.assertEqual(handler.redirect_url, "/settings/h3-cells")
        self.assertEqual(
            [(row["h3_cell"], row["name"]) for row in rows], [(h3_cell, "Oslo")]
        )
        self.assertIn("Oslo", body)
        self.assertIn(h3_cell, body)
        self.assertIn(
            f'<a href="https://h3geo.org/#hex={h3_cell}" target="_blank" rel="noopener">{h3_cell}</a>',
            body,
        )
        self.assertIn('<div class="custom-place-list h3-cell-list">', body)
        self.assertIn('<details class="custom-place-edit">', body)
        self.assertIn('<span class="status">1 bilder</span>', body)
        self.assertIn('<span class="status">H3-7</span>', body)
        self.assertIn(
            f'<input type="hidden" name="original_h3_cell" value="{h3_cell}">', body
        )
        self.assertIn(f'name="h3_cell" value="{h3_cell}"', body)
        self.assertIn('formaction="/settings/h3-cell-delete"', body)
        self.assertIn('data-confirm-submit="Slette navn gitt til H3-celle?"', body)
        self.assertIn(">Slett</button>", body)
        self.assertIn("event.submitter?.dataset.confirmSubmit", SERVER_JS)
        self.assertIn("event.preventDefault()", SERVER_JS)

    def test_run_server_h3_cells_page_updates_and_deletes_named_cell(self) -> None:
        original_cell = h3_cells_for_point(59.91273, 10.74609)["h3_res7"]
        updated_cell = h3_cells_for_point(60.39299, 5.32415)["h3_res7"]
        update_form = f"original_h3_cell={original_cell}&name=Bergen&h3_cell={updated_cell}".encode(
            "utf-8"
        )
        delete_form = f"original_h3_cell={updated_cell}&name=Bergen&h3_cell={updated_cell}".encode(
            "utf-8"
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            conn = db.connect(target)
            try:
                db.set_geo_place_name(conn, original_cell, "Oslo")
                conn.commit()
            finally:
                conn.close()

            class FakeHandler:
                server = SimpleNamespace(
                    target=target, face_enabled=True, openclip_enabled=True
                )
                redirect_url = ""

                def __init__(self, data: bytes) -> None:
                    self.headers = {
                        "Content-Length": str(len(data)),
                        "Content-Type": "application/x-www-form-urlencoded",
                    }
                    self.rfile = BytesIO(data)

                def redirect(self, url: str) -> None:
                    self.redirect_url = url

                def respond_html(
                    self, content: str, *, status: HTTPStatus = HTTPStatus.OK
                ) -> None:
                    raise AssertionError(
                        f"Unexpected error response {status}: {content}"
                    )

            update_handler = FakeHandler(update_form)
            server_endpoints_admin.respond_set_h3_cell_name(update_handler)  # type: ignore[arg-type]
            conn = db.connect(target)
            try:
                after_update = [
                    (row["h3_cell"], row["name"]) for row in db.geo_place_names(conn)
                ]
            finally:
                conn.close()

            delete_handler = FakeHandler(delete_form)
            server_endpoints_admin.respond_delete_h3_cell_name(delete_handler)  # type: ignore[arg-type]
            conn = db.connect(target)
            try:
                after_delete = [
                    (row["h3_cell"], row["name"]) for row in db.geo_place_names(conn)
                ]
            finally:
                conn.close()

        self.assertEqual(update_handler.redirect_url, "/settings/h3-cells")
        self.assertEqual(after_update, [(updated_cell, "Bergen")])
        self.assertEqual(delete_handler.redirect_url, "/settings/h3-cells")
        self.assertEqual(after_delete, [])

    def test_run_server_top_steder_link_points_to_geo_not_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")

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

        self.assertIn('<a class="server-search-link" href="/geo">Steder</a>', body)
        self.assertLess(body.index('href="/geo">Steder'), body.index('href="/search"'))

    def test_run_server_geo_pages_use_stored_geo_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(10, 10))
            (source / "IMG_20240103.png").write_bytes(minimal_png(11, 10))
            (source / "IMG_20240104.png").write_bytes(minimal_png(12, 10))
            (source / "IMG_20240105.png").write_bytes(minimal_png(13, 10))

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
            import h3

            cells = h3_cells_for_point(59.91273, 10.74609)
            neighbor_cell = next(
                cell
                for cell in sorted(h3.grid_disk(cells["h3_res7"], 1))
                if cell != cells["h3_res7"]
            )
            conn = db.connect(target)
            try:
                db.update_file_gps(
                    conn,
                    file_id=1,
                    gps_lat=59.91273,
                    gps_lon=10.74609,
                    gps_alt=None,
                    h3_cells=cells,
                    gps_source="test",
                    gps_error=None,
                )
                db.update_file_gps(
                    conn,
                    file_id=2,
                    gps_lat=59.91274,
                    gps_lon=10.74610,
                    gps_alt=None,
                    h3_cells=cells,
                    gps_source="test",
                    gps_error=None,
                )
                db.set_file_manual_h3_location(
                    conn, file_id=4, h3_cells=h3_cells_for_manual_cell(neighbor_cell)
                )
                db.set_geo_place_name(conn, cells["h3_res6"], "Oslo-området")
                conn.execute(
                    "UPDATE files SET deleted_at = CURRENT_TIMESTAMP WHERE id = 2"
                )
                conn.commit()
            finally:
                conn.close()

            index_body = geo_index_page_html(
                target, resolution=7, min_count=1, limit=10
            )
            map_body = geo_map_page_html(target, resolution=7, min_count=1, limit=10)
            map_zero_body = geo_map_page_html(
                target, resolution=0, min_count=1, limit=10
            )
            area_body = geo_area_page_html(
                target, cells["h3_res7"], resolution=7, limit=10
            )
            empty_area_body = geo_area_page_html(
                target, "8001fffffffffff", resolution=0, limit=10
            )

        self.assertIn("Steder", index_body)
        self.assertIn('href="/filter/missing%3Agps">Bilder uten GPS</a>', index_body)
        self.assertIn("/geo/map?resolution=7&min_count=1&limit=10", index_body)
        self.assertNotIn("/geo/stats", index_body)
        self.assertIn("Heksagonkart", map_body)
        self.assertIn(
            'href="/geo">Steder</a><span class="sep">/</span>Heksagonkart</nav>',
            map_body,
        )
        self.assertIn(
            f'href="/geo">Steder</a><span class="sep">/</span>{cells["h3_res7"]}</nav>',
            area_body,
        )
        self.assertIn(
            '<form action="/geo/map" method="get" class="geo-filter">', map_body
        )
        self.assertIn('<select name="resolution">', map_body)
        self.assertIn('<option value="7" selected>H3-7 (ca. 5 km²)</option>', map_body)
        self.assertIn(
            '<option value="0" selected>H3-0 (ca. 4 357 450 km²)</option>',
            map_zero_body,
        )
        self.assertIn(f'href="/geo/area/{cells["h3_res7"]}"', map_body)
        self.assertIn(f'href="/geo/area/{neighbor_cell}"', map_body)
        self.assertIn("geo-hex", map_body)
        self.assertIn(">1</text>", map_body)
        self.assertIn("IMG_20240102.png", area_body)
        self.assertIn("google.com/maps/@", area_body)
        self.assertIn(",11z", area_body)
        self.assertIn("Åpne i Google Maps (sentrum av H3-7)", area_body)
        self.assertIn('href="https://www.google.com/maps/@', empty_area_body)
        self.assertIn(",2z", empty_area_body)
        self.assertIn("Ingen aktive bilder i dette området.", empty_area_body)
        self.assertIn(
            'href="https://h3geo.org/#hex=' + cells["h3_res7"] + '"', area_body
        )
        self.assertIn(">H3Geo</a>", area_body)
        self.assertIn("oppløsning 7, ca. 5 km²", area_body)
        self.assertIn(f'href="/geo/area/{cells["h3_res6"]}"', area_body)
        self.assertIn("Større område: H3-6 Oslo-området", area_body)
        self.assertNotIn(f"Større område: H3-6 {cells['h3_res6']}", area_body)
        self.assertNotIn("IMG_20240103.png", area_body)

    def test_run_server_item_page_does_not_show_nearby_geo_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.png").write_bytes(minimal_png(10, 10))
            (source / "IMG_20240103.png").write_bytes(minimal_png(11, 10))

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
            cells = h3_cells_for_point(59.91273, 10.74609)
            conn = db.connect(target)
            try:
                for file_id in (1, 2):
                    db.update_file_gps(
                        conn,
                        file_id=file_id,
                        gps_lat=59.91273,
                        gps_lon=10.74609,
                        gps_alt=None,
                        h3_cells=cells,
                        gps_source="test",
                        gps_error=None,
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

        self.assertNotIn("Nærliggende bilder", body)
        self.assertNotIn("IMG_20240103.png", body)

    def test_run_server_item_page_omits_geo_info_without_h3_cells(self) -> None:
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
            body = image_info_content_html(target, item)

        self.assertNotIn("<dt>Steder</dt>", body)
        self.assertNotIn("<dt>Kart</dt>", body)
        self.assertNotIn("google.com/maps", body)
        self.assertNotIn("/geo/area/", body)
