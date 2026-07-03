from __future__ import annotations

import tempfile
import unittest
import datetime as dt
from pathlib import Path

from bildebank import db
from bildebank.geo import h3_cells_for_point
from bildebank.server_browser_sources import tag_browser_source
from bildebank.server_assets import SERVER_CSS
from bildebank.server_browser import (
    adjacent_browser_items,
    adjacent_source_items,
    browser_item_by_id,
    browser_month_items,
    browser_month_navigation,
    date_source_text,
    image_info_content_html,
    month_key_for_item,
    source_item_by_id,
    source_month_items,
    source_month_navigation,
)
from bildebank.server_geo import geo_component_pixel_coordinates
from bildebank.server_pages import item_page_html, source_item_page_html, source_month_page_html, tags_page_html
from bildebank.target_lock import LOCK_FILENAME
from tests.test_media import (
    jpeg_with_xmp_date,
    minimal_mp4_with_creation_date,
    minimal_tiff_with_datetime,
)
from tests.test_cli import (
    capture_cli,
    run_cli,
)


class MetadataTagsCliTests(unittest.TestCase):
    def test_geo_map_component_orientation_matches_cardinal_directions(self) -> None:
        import h3

        origin = h3_cells_for_point(59.91273, 10.74609)["h3_res7"]
        cells = sorted(h3.grid_disk(origin, 1))
        coords = geo_component_pixel_coordinates(cells, 28.0)
        origin_lat, origin_lon = h3.cell_to_latlng(origin)
        origin_x, origin_y = coords[origin]

        mismatches = 0
        comparisons = 0
        for cell in cells:
            if cell == origin:
                continue
            lat, lon = h3.cell_to_latlng(cell)
            x, y = coords[cell]
            if abs(lon - origin_lon) > 0.000001:
                comparisons += 1
                mismatches += (x > origin_x) != (lon > origin_lon)
            if abs(lat - origin_lat) > 0.000001:
                comparisons += 1
                mismatches += (y > origin_y) != (lat < origin_lat)

        self.assertGreater(comparisons, 0)
        self.assertLessEqual(mismatches, 1)

    def test_manual_date_changes_browser_month_without_moving_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20260102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            imported = target / "2026" / "01" / "IMG_20260102.jpg"

            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "date-set",
                    str(imported),
                    "--date",
                    "2004-07-15",
                    "--uncertainty",
                    "1m",
                    "--note",
                    "Kamera hadde feil dato",
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertTrue(imported.exists())
            self.assertIn("Manuell dato satt: ca. 2004-07-15", stdout)
            self.assertEqual(browser_month_items(target, "2026-01"), [])
            manual_items = browser_month_items(target, "2004-07")
            self.assertEqual([item["stored_filename"] for item in manual_items], ["IMG_20260102.jpg"])
            self.assertEqual(month_key_for_item(target, manual_items[0]), "2004-07")
            body = item_page_html(
                target,
                manual_items[0],
                *adjacent_browser_items(target, manual_items[0]),
                browser_month_navigation(target, manual_items[0]),
            )
            info_body = image_info_content_html(target, manual_items[0])
            self.assertNotIn("date-status-badge", body)
            self.assertNotIn("Opprinnelig: 2026-01-02", body)
            self.assertIn("ca. 2004-07-15", info_body)
            self.assertIn("Kamera hadde feil dato", info_body)
            self.assertIn("Opprinnelig dato", info_body)

            code, stdout, stderr = capture_cli(["--target", str(target), "date-clear", str(imported)])

            self.assertEqual(code, 0, stderr)
            self.assertEqual(browser_month_items(target, "2004-07"), [])
            self.assertEqual([item["stored_filename"] for item in browser_month_items(target, "2026-01")], ["IMG_20260102.jpg"])

    def test_manual_date_cli_commands_refuse_to_run_while_target_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20260102.jpg").write_bytes(b"image")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            imported = target / "2026" / "01" / "IMG_20260102.jpg"
            (target / LOCK_FILENAME).write_text("command=remove\n", encoding="utf-8")

            set_code, _set_stdout, set_stderr = capture_cli(
                ["--target", str(target), "date-set", str(imported), "--date", "2004-07-15"]
            )
            clear_code, _clear_stdout, clear_stderr = capture_cli(
                ["--target", str(target), "date-clear", str(imported)]
            )

        self.assertEqual(set_code, 1)
        self.assertIn("Bildesamlingen er låst", set_stderr)
        self.assertEqual(clear_code, 1)
        self.assertIn("Bildesamlingen er låst", clear_stderr)

    def test_image_info_date_source_labels_are_human_readable(self) -> None:
        self.assertEqual(date_source_text("metadata"), "fra metadata")
        self.assertEqual(date_source_text("filename"), "fra filnavn")
        self.assertEqual(date_source_text("mtime"), "fra mtime")

    def test_tag_add_stops_before_database_writes_when_target_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")
            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )
            lock_path = target / LOCK_FILENAME
            lock_path.write_text("command=remove\npid=123\n", encoding="utf-8")

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "tag-add", "2024/01/IMG_20240102.jpg", "Familie"]
            )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Bildesamlingen er låst", stderr)
            self.assertTrue(lock_path.exists())
            conn = db.connect(target)
            try:
                self.assertIsNone(conn.execute("SELECT id FROM tags WHERE name_key = 'familie'").fetchone())
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM command_log WHERE command = 'tag-add'").fetchone()[0],
                    0,
                )
            finally:
                conn.close()

    def test_tag_add_rolls_back_and_releases_lock_on_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image")
            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(
                run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]),
                0,
            )

            code, stdout, stderr = capture_cli(
                ["--target", str(target), "tag-add", "2024/01/IMG_20240102.jpg", ""]
            )

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Taggnavn kan ikke være tomt", stderr)
            self.assertFalse((target / LOCK_FILENAME).exists())
            conn = db.connect(target)
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM command_log WHERE command = 'tag-add'").fetchone()[0],
                    0,
                )
            finally:
                conn.close()

            self.assertEqual(
                run_cli(
                    [
                        "--target",
                        str(target),
                        "tag-add",
                        "2024/01/IMG_20240102.jpg",
                        "Familie",
                    ]
                ),
                0,
            )

    def test_tag_cli_and_server_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            source = Path(tmp) / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-a")
            (source / "IMG_20240203.jpg").write_bytes(b"image-b")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", "source", "--quiet", str(source)]), 0)
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            item_path = target / str(item["target_path"])

            code, stdout, stderr = capture_cli(["--target", str(target), "tag-add", str(item_path), "Familie"])
            self.assertEqual(code, 0, stderr)
            self.assertIn("La til: Familie", stdout)
            code, stdout, stderr = capture_cli(["--target", str(target), "tag-list"])
            self.assertEqual(code, 0, stderr)
            self.assertIn("Familie\t1\tuser", stdout)
            self.assertIn("Ute av fokus\t0\tsystem", stdout)
            code, stdout, stderr = capture_cli(["--target", str(target), "tag-files", "familie"])
            self.assertEqual(code, 0, stderr)
            self.assertIn("IMG_20240102.jpg", stdout)

            tag_source = tag_browser_source("familie")
            tag_item = source_item_by_id(target, tag_source, 1)
            self.assertIsNotNone(tag_item)
            self.assertIsNone(source_item_by_id(target, tag_source, 2))
            item_body = source_item_page_html(
                target,
                tag_source,
                tag_item,
                *adjacent_source_items(target, tag_source, tag_item),
                source_month_navigation(target, tag_source, tag_item),
            )
            tag_month = source_month_items(target, tag_source, "2024-01")
            month_body = source_month_page_html(target, tag_source, "2024-01", tag_month)
            tags_body = tags_page_html(target)

            code, stdout, stderr = capture_cli(["--target", str(target), "tag-remove", str(item_path), "familie"])
            self.assertEqual(code, 0, stderr)
            self.assertIn("Fjernet: familie", stdout)
            code, stdout, stderr = capture_cli(["--target", str(target), "tag-add", str(item_path), "Ute av fokus"])
            self.assertEqual(code, 0, stderr)
            self.assertIn("La til: Ute av fokus", stdout)
            code, stdout, stderr = capture_cli(["--target", str(target), "tag-list", str(item_path)])
            self.assertEqual(code, 0, stderr)
            self.assertIn("Ute av fokus\tsystem", stdout)
            code, stdout, stderr = capture_cli(["--target", str(target), "tag-remove", str(item_path), "Ute av fokus"])
            self.assertEqual(code, 0, stderr)
            self.assertIn("Fjernet: Ute av fokus", stdout)
            code, stdout, stderr = capture_cli(["--target", str(target), "tag-list"])
            self.assertEqual(code, 0, stderr)
            self.assertNotIn("Familie", stdout)
            self.assertIn("Ute av fokus\t0\tsystem", stdout)

        self.assertIn("Tagg: familie", item_body)
        self.assertIn('href="/item/1">Åpne i alle bilder</a>', item_body)
        self.assertIn('href="/tag/familie/year/2024">2024</a>', item_body)
        self.assertIn('href="/tag/familie/item/1"', month_body)
        self.assertEqual(len(tag_month), 1)
        self.assertIn("<h1>Tagger</h1>", tags_body)
        self.assertIn('action="/tags/create"', tags_body)
        self.assertIn('action="/tags/rename"', tags_body)
        self.assertIn('action="/tags/delete"', tags_body)
        self.assertIn('class="tag-actions"', tags_body)
        self.assertIn(".tag-actions", SERVER_CSS)
        self.assertIn('data-confirm-submit="Slette taggen Familie fra alle bilder?"', tags_body)
        self.assertIn("systemtagg kan ikke endres", tags_body)
        self.assertIn('href="/tag/Familie">Vis bilder (1)</a>', tags_body)
        self.assertIn("brukertagg", tags_body)
        self.assertIn("systemtagg", tags_body)

    def test_non_metadata_lists_files_not_placed_by_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "video.mp4").write_bytes(minimal_mp4_with_creation_date(dt.date(2010, 7, 8)))
            (source / "IMG_20240102.jpg").write_bytes(b"filename-date")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            code, stdout, stderr = capture_cli(["--target", str(target), "non-metadata"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("filename\t2024-01-02", stdout)
            self.assertIn("IMG_20240102.jpg", stdout)
            self.assertNotIn("video.mp4", stdout)

    def test_explain_date_shows_selected_date_and_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "IMG_20240102.jpg"
            path.write_bytes(b"not-a-real-jpeg")

            code, stdout, stderr = capture_cli(["explain-date", str(path)])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Valgt dato: 2024-01-02", stdout)
            self.assertIn("Valgt kilde: filename", stdout)
            self.assertIn("JPEG EXIF", stdout)
            self.assertIn("Dato i filnavn", stdout)

    def test_inspect_metadata_shows_metadata_fragments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "xmp-only.jpg"
            path.write_bytes(jpeg_with_xmp_date("2007-03-12T19:54:18+01:00"))

            code, stdout, stderr = capture_cli(["inspect-metadata", str(path)])

            self.assertEqual(code, 0, stderr)
            self.assertIn("JPEG metadata:", stdout)
            self.assertIn("APP1", stdout)
            self.assertIn("XMP dato: 2007-03-12", stdout)

    def test_inspect_metadata_shows_tiff_raw_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "DSC_0170.NEF"
            path.write_bytes(minimal_tiff_with_datetime("2019:03:03 12:00:00"))

            code, stdout, stderr = capture_cli(["inspect-metadata", str(path)])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Valgt dato: 2019-03-03", stdout)
            self.assertIn("TIFF/RAW metadata:", stdout)
            self.assertIn("TIFF/RAW dato: 2019-03-03", stdout)
