from __future__ import annotations

import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from bilder import db
from bilder.cli import main
from bilder.db import init_database
from bilder.geo import (
    extract_gps_from_metadata,
    h3_cells_for_point,
    h3_area_label,
    h3_column_for_resolution,
    h3_resolution_label,
    scan_geo,
)
from bilder.media import sha256_file


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

        self.assertEqual(set(cells), {"h3_res5", "h3_res6", "h3_res7", "h3_res8", "h3_res9"})
        self.assertTrue(all(cells.values()))

    def test_h3_column_for_resolution_rejects_unsupported_resolution(self) -> None:
        with self.assertRaises(ValueError):
            h3_column_for_resolution(4)

    def test_h3_area_labels_are_available_for_supported_resolutions(self) -> None:
        self.assertEqual(h3_area_label(7), "ca. 5 km²")
        self.assertEqual(h3_resolution_label(8), "oppløsning 8, ca. 0,7 km²")
        with self.assertRaises(ValueError):
            h3_area_label(4)

    def test_geo_columns_are_added_to_new_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            conn = db.connect(target)
            try:
                columns = db.table_columns(conn, "files")
            finally:
                conn.close()

        self.assertIn("gps_lat", columns)
        self.assertIn("h3_res7", columns)
        self.assertIn("gps_scanned_at", columns)

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

            with patch("bilder.geo.read_gps_metadata_batch", fake_read_gps_metadata_batch):
                with redirect_stderr(StringIO()):
                    stats = scan_geo(target, exiftool_path="exiftool", batch_size=1)

            self.assertEqual(stats.checked, 1)
            self.assertEqual(stats.with_gps, 1)
            self.assertEqual(seen_batches, [[expected_path]])

            conn = db.connect(target)
            try:
                row = conn.execute("SELECT gps_lat, gps_lon, h3_res7 FROM files").fetchone()
            finally:
                conn.close()

        self.assertEqual(float(row["gps_lat"]), 59.91273)
        self.assertEqual(float(row["gps_lon"]), 10.74609)
        self.assertTrue(row["h3_res7"])

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
