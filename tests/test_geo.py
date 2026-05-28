from __future__ import annotations

import tempfile
import unittest
import urllib.parse
import uuid
from contextlib import redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.cli import main
from bildebank.db import init_database
from bildebank.geo import (
    PREDEFINED_GEO_PLACES,
    PredefinedGeoPlace,
    extract_gps_from_metadata,
    h3_cells_for_point,
    h3_cells_for_manual_cell,
    h3_area_label,
    h3_column_for_resolution,
    h3_resolution,
    h3_resolution_label,
    scan_geo,
)
from bildebank.media import sha256_file
from bildebank.server import (
    BildebankRequestHandler,
    resolve_doc_path,
)
from bildebank.server_pages import (
    custom_geo_places_page_html,
    geo_area_page_html,
    geo_index_page_html,
    markdown_doc_page_html,
    source_month_page_html,
)
from bildebank.server_browser import (
    adjacent_source_items,
    source_item_by_id,
    source_month_items,
    source_month_navigation,
)
from bildebank.server_browser_sources import geo_place_browser_source, source_item_url
from bildebank.server_geo import geo_place_by_slug, geo_place_cells_by_column, geo_place_items
from bildebank.server_markdown import markdown_to_html


def capture_cli(args: list[str]) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(args)
    return code, stdout.getvalue(), stderr.getvalue()


def register_target_file(target: Path, relative_path: Path, *, content: bytes = b"image") -> int:
    path = target / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    source = target / "_source" / path.name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    conn = db.connect(target)
    try:
        source_id = db.add_named_source(conn, source.parent, f"source-{uuid.uuid4()}")
        file_id = db.insert_imported_file(
            conn,
            source_id=source_id,
            source_path=source,
            target_root=target,
            target_path=path,
            original_filename=path.name,
            stored_filename=path.name,
            sha256=sha256_file(path),
            size_bytes=path.stat().st_size,
            taken_date="2024-01-02",
            date_source="filename",
            name_conflict=False,
        )
        conn.commit()
        return file_id
    finally:
        conn.close()


def set_file_h3_cells(target: Path, file_id: int, cells: dict[str, str]) -> None:
    conn = db.connect(target)
    try:
        assignments = ", ".join(f"{column} = ?" for column in cells)
        conn.execute(f"UPDATE files SET {assignments} WHERE id = ?", (*cells.values(), file_id))
        conn.commit()
    finally:
        conn.close()


class GeoTests(unittest.TestCase):
    def test_extract_gps_from_metadata_returns_none_without_gps(self) -> None:
        self.assertIsNone(extract_gps_from_metadata({"SourceFile": "image.jpg"}))

    def test_extract_gps_from_metadata_returns_decimal_coordinates(self) -> None:
        gps = extract_gps_from_metadata(
            {"GPSLatitude": "59.91273", "GPSLongitude": "10.74609", "GPSAltitude": "12.5"}
        )

        self.assertIsNotNone(gps)
        assert gps is not None
        self.assertEqual(gps.lat, 59.91273)
        self.assertEqual(gps.lon, 10.74609)
        self.assertEqual(gps.alt, 12.5)

    def test_extract_gps_from_metadata_rejects_invalid_latitude(self) -> None:
        with self.assertRaises(ValueError):
            extract_gps_from_metadata({"GPSLatitude": "91", "GPSLongitude": "10"})

    def test_extract_gps_from_metadata_rejects_invalid_longitude(self) -> None:
        with self.assertRaises(ValueError):
            extract_gps_from_metadata({"GPSLatitude": "59", "GPSLongitude": "181"})

    def test_h3_cells_for_point_returns_supported_resolutions(self) -> None:
        cells = h3_cells_for_point(59.91273, 10.74609)

        self.assertEqual(set(cells), {f"h3_res{resolution}" for resolution in range(12)})
        self.assertTrue(all(cells.values()))

    def test_h3_cells_for_manual_cell_keeps_only_selected_cell_and_parents(self) -> None:
        import h3

        cell = h3.latlng_to_cell(59.91273, 10.74609, 3)
        cells = h3_cells_for_manual_cell(cell)

        self.assertEqual(cells["h3_res0"], h3.cell_to_parent(cell, 0))
        self.assertEqual(cells["h3_res2"], h3.cell_to_parent(cell, 2))
        self.assertEqual(cells["h3_res3"], cell)
        self.assertIsNone(cells["h3_res4"])
        self.assertIsNone(cells["h3_res11"])

    def test_h3_column_for_resolution_rejects_unsupported_resolution(self) -> None:
        self.assertEqual(h3_column_for_resolution(11), "h3_res11")
        with self.assertRaises(ValueError):
            h3_column_for_resolution(12)

    def test_h3_area_labels_are_available_for_supported_resolutions(self) -> None:
        self.assertEqual(h3_area_label(0), "ca. 4 357 450 km²")
        self.assertEqual(h3_area_label(7), "ca. 5 km²")
        self.assertEqual(h3_resolution_label(8), "oppløsning 8, ca. 0,7 km²")
        self.assertEqual(h3_resolution_label(11), "oppløsning 11, ca. 0,002 km²")
        with self.assertRaises(ValueError):
            h3_area_label(12)

    def test_geo_columns_are_added_to_new_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            conn = db.connect(target)
            try:
                columns = db.table_columns(conn, "files")
                indexes = {str(row["name"]) for row in conn.execute("PRAGMA index_list(files)")}
            finally:
                conn.close()

        self.assertIn("gps_lat", columns)
        self.assertIn("h3_res0", columns)
        self.assertIn("h3_res7", columns)
        self.assertIn("h3_res9", columns)
        self.assertIn("h3_res10", columns)
        self.assertIn("h3_res11", columns)
        self.assertIn("gps_scanned_at", columns)
        self.assertIn("idx_files_h3_res7", indexes)
        self.assertIn("idx_files_h3_res7_browser_order", indexes)
        self.assertIn("idx_files_h3_res11", indexes)
        self.assertIn("idx_files_h3_res11_browser_order", indexes)

    def test_custom_geo_place_tables_are_added_to_new_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            conn = db.connect(target)
            try:
                tables = {
                    str(row["name"])
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                }
            finally:
                conn.close()

        self.assertIn("geo_places", tables)
        self.assertIn("geo_place_cells", tables)

    def test_geo_areas_filters_deleted_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            active_id = register_target_file(target, Path("2024/01/active.jpg"))
            deleted_id = register_target_file(target, Path("2024/01/deleted.jpg"))
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
                conn.execute("UPDATE files SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", (deleted_id,))
                conn.commit()

                areas = db.geo_areas(conn, column="h3_res7", min_count=1, limit=10)
                files = db.geo_area_files(conn, column="h3_res7", h3_cell=cells["h3_res7"])
            finally:
                conn.close()

        self.assertEqual(len(areas), 1)
        self.assertEqual(int(areas[0]["count"]), 1)
        self.assertEqual([row["target_path"] for row in files], ["2024/01/active.jpg"])

    def test_geo_place_name_can_be_saved_and_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            h3_cell = h3_cells_for_point(59.91273, 10.74609)["h3_res7"]
            conn = db.connect(target)
            try:
                saved = db.set_geo_place_name(conn, h3_cell, "  Hytta  ")
                self.assertEqual(saved, "Hytta")
                self.assertEqual(db.geo_place_name(conn, h3_cell), "Hytta")

                cleared = db.set_geo_place_name(conn, h3_cell, " ")
                self.assertIsNone(cleared)
                self.assertIsNone(db.geo_place_name(conn, h3_cell))
            finally:
                conn.close()

    def test_geo_area_page_shows_place_name_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            h3_cell = h3_cells_for_point(59.91273, 10.74609)["h3_res7"]
            conn = db.connect(target)
            try:
                db.set_geo_place_name(conn, h3_cell, "Hytta")
                conn.commit()
            finally:
                conn.close()

            html = geo_area_page_html(target, h3_cell, resolution=7, limit=25)

        self.assertIn("<h1>Hytta</h1>", html)
        self.assertIn('action="/geo/place-name"', html)
        self.assertIn('name="name" value="Hytta"', html)
        self.assertIn(f'name="h3_cell" value="{h3_cell}"', html)

    def test_geo_index_page_uses_saved_place_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            file_id = register_target_file(target, Path("2024/01/active.jpg"))
            cells = h3_cells_for_point(59.91273, 10.74609)
            conn = db.connect(target)
            try:
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
                db.set_geo_place_name(conn, cells["h3_res7"], "Hytta")
                conn.commit()
            finally:
                conn.close()

            html = geo_index_page_html(target, resolution=7, min_count=1, limit=10)

        self.assertIn("Hytta", html)
        self.assertIn(cells["h3_res7"], html)

    def test_predefined_geo_place_finds_cells_across_resolutions_without_duplicates(self) -> None:
        place = PREDEFINED_GEO_PLACES[0]
        first_cell = place.h3_cells[0]
        second_cell = place.h3_cells[2]
        first_column = h3_column_for_resolution(h3_resolution(first_cell))
        second_column = h3_column_for_resolution(h3_resolution(second_cell))

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            first_id = register_target_file(target, Path("2024/01/first.jpg"), content=b"first")
            second_id = register_target_file(target, Path("2024/01/second.jpg"), content=b"second")
            duplicate_match_id = register_target_file(target, Path("2024/01/both.jpg"), content=b"both")
            outside_id = register_target_file(target, Path("2024/01/outside.jpg"), content=b"outside")

            set_file_h3_cells(target, first_id, {first_column: first_cell})
            set_file_h3_cells(target, second_id, {second_column: second_cell})
            set_file_h3_cells(target, duplicate_match_id, {first_column: first_cell, second_column: second_cell})
            set_file_h3_cells(target, outside_id, {first_column: "831ec9fffffffff"})

            items = geo_place_items(target, place.slug)

            conn = db.connect(target)
            try:
                count = db.geo_place_count(conn, cells_by_column=geo_place_cells_by_column(place))
            finally:
                conn.close()

        item_ids = [int(item["id"]) for item in items]
        self.assertEqual(set(item_ids), {first_id, second_id, duplicate_match_id})
        self.assertEqual(len(item_ids), len(set(item_ids)))
        self.assertEqual(count, 3)

    def test_custom_geo_place_uses_same_browser_flow_as_predefined_places(self) -> None:
        import h3

        parent_cell = h3.latlng_to_cell(59.91273, 10.74609, 7)
        child_cell = sorted(h3.cell_to_children(parent_cell, 8))[0]
        parent_column = h3_column_for_resolution(7)
        child_column = h3_column_for_resolution(8)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            parent_id = register_target_file(target, Path("2024/01/01-parent.jpg"), content=b"parent")
            child_id = register_target_file(target, Path("2024/01/02-child.jpg"), content=b"child")
            duplicate_id = register_target_file(target, Path("2024/01/03-both.jpg"), content=b"both")
            outside_id = register_target_file(target, Path("2024/01/04-outside.jpg"), content=b"outside")
            set_file_h3_cells(target, parent_id, {parent_column: parent_cell})
            set_file_h3_cells(target, child_id, {child_column: child_cell})
            set_file_h3_cells(target, duplicate_id, {parent_column: parent_cell, child_column: child_cell})
            set_file_h3_cells(target, outside_id, {parent_column: "871ec9fffffffff"})
            conn = db.connect(target)
            try:
                db.set_custom_geo_place(
                    conn,
                    slug="hytta",
                    name="Hytta",
                    h3_cells=[parent_cell, child_cell],
                )
                conn.commit()
            finally:
                conn.close()

            place = geo_place_by_slug(target, "hytta")
            assert place is not None
            source = geo_place_browser_source(place)
            with patch("bildebank.server_browser.source_items", side_effect=AssertionError("source_items should not be used")):
                items = geo_place_items(target, "hytta")
                item = source_item_by_id(target, source, parent_id)
                assert item is not None
                previous_item, next_item = adjacent_source_items(target, source, item)
                month_items = source_month_items(target, source, "2024-01")

        item_ids = [int(item["id"]) for item in items]
        self.assertEqual(set(item_ids), {parent_id, child_id, duplicate_id})
        self.assertEqual(len(item_ids), len(set(item_ids)))
        self.assertIsNone(previous_item)
        self.assertEqual(int(next_item["id"]), child_id)
        self.assertEqual([int(item["id"]) for item in month_items], [parent_id, child_id, duplicate_id])
        self.assertEqual(source_item_url(source, parent_id), f"/geo/place/hytta/item/{parent_id}")

    def test_geo_place_cells_above_stored_resolution_match_h3_res11_parent(self) -> None:
        import h3

        high_resolution_cell = h3.latlng_to_cell(59.91273, 10.74609, 12)
        place = PredefinedGeoPlace("test", "Test", (high_resolution_cell,))
        parent = h3.cell_to_parent(high_resolution_cell, 11)

        self.assertEqual(geo_place_cells_by_column(place), [("h3_res11", parent)])

    def test_geo_index_page_lists_predefined_and_custom_places_with_count(self) -> None:
        place = PREDEFINED_GEO_PLACES[0]
        h3_cell = place.h3_cells[0]
        column = h3_column_for_resolution(h3_resolution(h3_cell))

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            file_id = register_target_file(target, Path("2024/01/kreta.jpg"))
            set_file_h3_cells(target, file_id, {column: h3_cell})
            conn = db.connect(target)
            try:
                db.set_custom_geo_place(conn, slug="min_plass", name="Min plass", h3_cells=[h3_cell])
                conn.commit()
            finally:
                conn.close()

            html = geo_index_page_html(target, resolution=7, min_count=1, limit=10)

        self.assertIn("<h2>Definerte steder</h2>", html)
        self.assertIn("Kreta", html)
        self.assertIn('href="/geo/place/kreta"', html)
        self.assertIn(
            'href="https://h3geo.org/#hex=' + urllib.parse.quote_plus(", ".join(place.h3_cells)) + '"',
            html,
        )
        self.assertIn("Min plass", html)
        self.assertIn('href="/geo/place/min_plass"', html)
        self.assertIn('href="https://h3geo.org/#hex=' + urllib.parse.quote_plus(h3_cell) + '"', html)
        self.assertIn('target="_blank"', html)
        self.assertIn('rel="noopener"', html)
        self.assertIn('href="/geo/custom-places"', html)
        self.assertIn('href="/help/web/steder"', html)
        self.assertNotIn('action="/geo/custom-place"', html)
        self.assertNotIn('action="/geo/custom-place-delete"', html)
        self.assertIn("<strong>1 bilder</strong>", html)

    def test_geo_help_markdown_is_rendered_as_html(self) -> None:
        doc_path = resolve_doc_path("web/steder")
        self.assertIsNotNone(doc_path)
        assert doc_path is not None

        html = markdown_doc_page_html(doc_path, doc_path.read_text(encoding="utf-8"))

        self.assertIn("<h1>Bruk av GPS-lokasjon i bilder</h1>", html)
        self.assertIn("<code>/geo</code>", html)
        self.assertIn("<strong>Egendefinerte steder</strong>", html)
        self.assertIn("statistikk over bilder med og uten GPS-lokasjon", html)
        self.assertNotIn("# Steder", html)

    def test_markdown_help_renderer_omits_cli_help_markers(self) -> None:
        html = markdown_to_html(
            """# Import

<!-- CLI-HELP-START -->
usage: bildebank import [valg]
<!-- CLI-HELP-END -->

Vanlig dokumentasjon.
"""
        )

        self.assertIn("<h1>Import</h1>", html)
        self.assertIn("Vanlig dokumentasjon.", html)
        self.assertIn("usage: bildebank import", html)
        self.assertNotIn("CLI-HELP-START", html)
        self.assertNotIn("CLI-HELP-END", html)

    def test_markdown_help_renderer_supports_numbered_lists(self) -> None:
        html = markdown_to_html(
            """Først:

1. Les `README`
2. Kjør **import**
3. Se [hjelpen](help.md)

- Ferdig
"""
        )

        self.assertIn(
            '<ol><li>Les <code>README</code></li><li>Kjør <strong>import</strong></li><li>Se <a href="help.md">hjelpen</a></li></ol>',
            html,
        )
        self.assertIn("<ul><li>Ferdig</li></ul>", html)

    def test_markdown_help_renderer_supports_wrapped_list_items(self) -> None:
        html = markdown_to_html(
            """- Første punkt går
  over flere linjer
- Andre punkt

1. Nummerert punkt går
   også over flere linjer
2. Siste punkt
"""
        )

        self.assertIn(
            "<ul><li>Første punkt går over flere linjer</li><li>Andre punkt</li></ul>",
            html,
        )
        self.assertIn(
            "<ol><li>Nummerert punkt går også over flere linjer</li><li>Siste punkt</li></ol>",
            html,
        )

    def test_custom_geo_places_page_has_edit_forms(self) -> None:
        place = PREDEFINED_GEO_PLACES[0]
        h3_cell = place.h3_cells[0]

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            conn = db.connect(target)
            try:
                db.set_custom_geo_place(conn, slug="min_plass", name="Min plass", h3_cells=[h3_cell])
                conn.commit()
            finally:
                conn.close()

            html = custom_geo_places_page_html(target)

        self.assertIn("<h1>Egne steder</h1>", html)
        self.assertIn('action="/geo/custom-place"', html)
        self.assertIn('action="/geo/custom-place-delete"', html)
        self.assertIn('name="original_slug" value="min_plass"', html)
        self.assertIn('name="slug" value="min_plass"', html)
        self.assertIn('name="name" value="Min plass"', html)
        self.assertIn(h3_cell, html)

    def test_custom_geo_place_slug_change_renames_existing_place(self) -> None:
        place = PREDEFINED_GEO_PLACES[0]
        old_cell = place.h3_cells[0]
        new_cell = place.h3_cells[1]

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            conn = db.connect(target)
            try:
                db.set_custom_geo_place(conn, slug="gammel_plass", name="Gammel plass", h3_cells=[old_cell])
                conn.commit()
            finally:
                conn.close()

            body = urllib.parse.urlencode(
                {
                    "original_slug": "gammel_plass",
                    "slug": "ny_plass",
                    "name": "Ny plass",
                    "h3_cells": new_cell,
                }
            ).encode("utf-8")
            handler = object.__new__(BildebankRequestHandler)
            handler.headers = {"Content-Length": str(len(body))}  # type: ignore[assignment]
            handler.rfile = BytesIO(body)  # type: ignore[assignment]
            handler.server = type(
                "Server",
                (),
                {"target": target, "face_enabled": True, "openclip_enabled": True},
            )()  # type: ignore[attr-defined]
            redirect: dict[str, str] = {}

            def fake_redirect(url: str) -> None:
                redirect["url"] = url

            handler.redirect = fake_redirect  # type: ignore[method-assign]

            handler.respond_set_custom_geo_place()

            conn = db.connect(target)
            try:
                old_place = db.custom_geo_place(conn, "gammel_plass")
                new_place = db.custom_geo_place(conn, "ny_plass")
            finally:
                conn.close()

        self.assertIsNone(old_place)
        self.assertIsNotNone(new_place)
        assert new_place is not None
        self.assertEqual(new_place["name"], "Ny plass")
        self.assertEqual(new_place["h3_cells"], (new_cell,))
        self.assertEqual(redirect["url"], "/geo/custom-places")

    def test_geo_place_item_and_month_pages_use_browser_source_urls(self) -> None:
        place = PREDEFINED_GEO_PLACES[0]
        h3_cell = place.h3_cells[0]
        column = h3_column_for_resolution(h3_resolution(h3_cell))

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            first_id = register_target_file(target, Path("2024/01/first.jpg"), content=b"first")
            second_id = register_target_file(target, Path("2024/01/second.jpg"), content=b"second")
            set_file_h3_cells(target, first_id, {column: h3_cell})
            set_file_h3_cells(target, second_id, {column: h3_cell})
            source = geo_place_browser_source(place)
            with patch("bildebank.server_browser.source_items", side_effect=AssertionError("source_items should not be used")):
                item = source_item_by_id(target, source, first_id)
                assert item is not None
                previous_item, next_item = adjacent_source_items(target, source, item)
                month_items = source_month_items(target, source, "2024-01")
                month_navigation = source_month_navigation(target, source, item)
            assert item is not None
            month_html = source_month_page_html(target, source, "2024-01", month_items)

        self.assertIsNone(previous_item)
        self.assertEqual(int(next_item["id"]), second_id)
        self.assertEqual(month_navigation["previous_month"], None)
        self.assertEqual(source_item_url(source, first_id), "/geo/place/kreta/item/" + str(first_id))
        self.assertIn('href="/geo/place/kreta/item/', month_html)
        self.assertIn("Månedsoversikt: 2024-01", month_html)

    def test_unknown_geo_place_slug_returns_404(self) -> None:
        response: dict[str, object] = {}
        handler = object.__new__(BildebankRequestHandler)
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        target = Path(tempdir.name) / "target"
        init_database(target)
        handler.server = type("Server", (), {"target": target})()  # type: ignore[attr-defined]

        def fake_respond_text(content: str, *, status: object) -> None:
            response["content"] = content
            response["status"] = status

        handler.respond_text = fake_respond_text  # type: ignore[method-assign]

        handler.respond_geo_place("ukjent")

        self.assertEqual(getattr(response["status"], "value", None), 404)
        self.assertEqual(response["content"], "Ukjent sted.")

    def test_geo_area_page_links_to_child_areas_with_saved_names(self) -> None:
        import h3

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            first_file_id = register_target_file(target, Path("2024/01/first.jpg"))
            second_file_id = register_target_file(target, Path("2024/01/second.jpg"))
            third_file_id = register_target_file(target, Path("2024/01/third.jpg"))
            cells = h3_cells_for_point(59.91273, 10.74609)
            parent_cell = cells["h3_res7"]
            child_cells = sorted(h3.cell_to_children(parent_cell, 8))
            first_child = child_cells[0]
            second_child = child_cells[1]

            conn = db.connect(target)
            try:
                for file_id, child_cell in (
                    (first_file_id, first_child),
                    (second_file_id, first_child),
                    (third_file_id, second_child),
                ):
                    file_cells = dict(cells)
                    file_cells["h3_res8"] = child_cell
                    db.update_file_gps(
                        conn,
                        file_id=file_id,
                        gps_lat=59.91273,
                        gps_lon=10.74609,
                        gps_alt=None,
                        h3_cells=file_cells,
                        gps_source="test",
                        gps_error=None,
                    )
                db.set_geo_place_name(conn, parent_cell, "Hytta")
                db.set_geo_place_name(conn, first_child, "Brygga")
                conn.commit()
            finally:
                conn.close()

            html = geo_area_page_html(target, parent_cell, resolution=7, limit=10)

        first_link = f'href="/geo/area/{first_child}"'
        second_link = f'href="/geo/area/{second_child}"'
        self.assertIn("<h2>Inneholder</h2>", html)
        self.assertIn("Understeder på H3-oppløsning 8", html)
        self.assertIn("Brygga", html)
        self.assertIn(first_link, html)
        self.assertIn("Hytta (arvet)", html)
        self.assertIn(second_link, html)
        self.assertLess(html.index("Brygga"), html.index("Hytta (arvet)"))
        self.assertIn("<strong>2 bilder</strong>", html)
        self.assertIn("<strong>1 bilder</strong>", html)

    def test_geo_area_page_omits_child_areas_for_highest_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            cells = h3_cells_for_point(59.91273, 10.74609)

            html = geo_area_page_html(target, cells["h3_res9"], resolution=9, limit=10)

        self.assertNotIn("<h2>Inneholder</h2>", html)

    def test_geo_scan_uses_relative_target_path_under_collection_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            register_target_file(target, Path("2024/01/image.jpg"))
            expected_path = target / "2024/01/image.jpg"
            seen_batches: list[list[Path]] = []

            def fake_read_gps_metadata_batch(exiftool_path: Path | str, paths: list[Path]) -> dict[Path, dict[str, object]]:
                seen_batches.append(paths)
                return {
                    expected_path: {
                        "SourceFile": str(expected_path),
                        "GPSLatitude": 59.91273,
                        "GPSLongitude": 10.74609,
                    }
                }

            with patch("bildebank.geo.read_gps_metadata_batch", fake_read_gps_metadata_batch):
                with redirect_stderr(StringIO()):
                    stats = scan_geo(target, exiftool_path="exiftool", batch_size=1)

            self.assertEqual(stats.checked, 1)
            self.assertEqual(stats.with_gps, 1)
            self.assertEqual(seen_batches, [[expected_path]])

            conn = db.connect(target)
            try:
                row = conn.execute("SELECT gps_lat, gps_lon, h3_res7, h3_res11 FROM files").fetchone()
            finally:
                conn.close()

        self.assertEqual(float(row["gps_lat"]), 59.91273)
        self.assertEqual(float(row["gps_lon"]), 10.74609)
        self.assertTrue(row["h3_res7"])
        self.assertTrue(row["h3_res11"])

    def test_geo_scan_does_not_update_files_when_batch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            register_target_file(target, Path("2024/01/image.jpg"))
            long_error = "\n".join(f"Error: File not found - image-{index}.jpg" for index in range(200))

            def fake_read_gps_metadata_batch(exiftool_path: Path | str, paths: list[Path]) -> dict[Path, dict[str, object]]:
                raise RuntimeError(long_error)

            with patch("bildebank.geo.read_gps_metadata_batch", fake_read_gps_metadata_batch):
                with redirect_stderr(StringIO()):
                    stats = scan_geo(target, exiftool_path="exiftool", batch_size=1)

            conn = db.connect(target)
            try:
                row = conn.execute("SELECT gps_error FROM files").fetchone()
            finally:
                conn.close()

        self.assertEqual(stats.errors, 1)
        self.assertEqual(stats.updated, 0)
        self.assertIsNone(row["gps_error"])

    def test_geo_scan_marks_per_file_exiftool_errors_without_storing_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            register_target_file(target, Path("2024/01/image.jpg"))
            expected_path = target / "2024/01/image.jpg"

            def fake_read_gps_metadata_batch(exiftool_path: Path | str, paths: list[Path]) -> dict[Path, dict[str, object]]:
                return {expected_path: {"SourceFile": str(expected_path), "Error": "unsupported file type with long details"}}

            with patch("bildebank.geo.read_gps_metadata_batch", fake_read_gps_metadata_batch):
                with redirect_stderr(StringIO()):
                    stats = scan_geo(target, exiftool_path="exiftool", batch_size=1)

            conn = db.connect(target)
            try:
                row = conn.execute("SELECT gps_error FROM files").fetchone()
            finally:
                conn.close()

        self.assertEqual(stats.errors, 1)
        self.assertEqual(stats.updated, 1)
        self.assertEqual(row["gps_error"], db.GPS_ERROR_EXIFTOOL)

    def test_geo_scan_marks_missing_target_file_without_storing_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            register_target_file(target, Path("2024/01/image.jpg"))
            (target / "2024/01/image.jpg").unlink()

            with redirect_stderr(StringIO()):
                stats = scan_geo(target, exiftool_path="exiftool", batch_size=1)

            conn = db.connect(target)
            try:
                row = conn.execute("SELECT gps_error FROM files").fetchone()
            finally:
                conn.close()

        self.assertEqual(stats.errors, 1)
        self.assertEqual(stats.updated, 1)
        self.assertEqual(row["gps_error"], db.GPS_ERROR_FILE_MISSING)

    def test_geo_scan_skips_manual_h3_locations_even_with_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            file_id = register_target_file(target, Path("2024/01/image.jpg"))
            cells = h3_cells_for_point(59.91273, 10.74609)
            conn = db.connect(target)
            try:
                db.set_file_manual_h3_location(
                    conn,
                    file_id=file_id,
                    h3_cells=cells,
                )
                db.set_custom_geo_place(conn, slug="manuell", name="Manuell", h3_cells=[cells["h3_res7"]])
                area_files = db.geo_area_files(conn, column="h3_res7", h3_cell=cells["h3_res7"])
                conn.commit()
            finally:
                conn.close()
            place_items = geo_place_items(target, "manuell")

            def fail_read_gps_metadata_batch(exiftool_path: Path | str, paths: list[Path]) -> dict[Path, dict[str, object]]:
                raise AssertionError("manual-h3 files should not be scanned")

            with patch("bildebank.geo.read_gps_metadata_batch", fail_read_gps_metadata_batch):
                with redirect_stderr(StringIO()):
                    normal_stats = scan_geo(target, exiftool_path="exiftool", batch_size=1)
                    force_stats = scan_geo(target, force=True, exiftool_path="exiftool", batch_size=1)

            conn = db.connect(target)
            try:
                row = conn.execute("SELECT gps_source, gps_lat, gps_lon, gps_alt, h3_res11 FROM files WHERE id = ?", (file_id,)).fetchone()
            finally:
                conn.close()

        self.assertEqual(normal_stats.checked, 0)
        self.assertEqual(force_stats.checked, 0)
        self.assertEqual(row["gps_source"], "manual-h3")
        self.assertIsNone(row["gps_lat"])
        self.assertIsNone(row["gps_lon"])
        self.assertIsNone(row["gps_alt"])
        self.assertEqual(row["h3_res11"], cells["h3_res11"])
        self.assertEqual([int(row["id"]) for row in area_files], [file_id])
        self.assertEqual([int(row["id"]) for row in place_items], [file_id])

    def test_geo_stats_cli_reports_active_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            register_target_file(target, Path("2024/01/active.jpg"))
            deleted_id = register_target_file(target, Path("2024/01/deleted.jpg"))
            conn = db.connect(target)
            try:
                conn.execute("UPDATE files SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", (deleted_id,))
                conn.commit()
            finally:
                conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "geo-stats"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("Images total:             1", stdout)
