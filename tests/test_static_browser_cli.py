from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.cli import main
from bildebank.db import DB_FILENAME, init_database
from bildebank.face import connect_face_db
from bildebank.html_export import render_html
from bildebank.server_assets import SERVER_CSS
from bildebank.server_browser_queries import browser_month_items, source_item_ids
from bildebank.server_browser_sources import all_browser_source
from bildebank.server_pages import month_page_html
from bildebank.target_lock import LOCK_FILENAME
from bildebank.thumbnails import (
    existing_thumbnail_url,
    thumbnail_absolute_path,
    thumbnail_is_current,
    thumbnail_relative_path,
)
from tests.cli_helpers import capture_cli, run_cli, write_test_image
from tests.db_test_helpers import register_target_file
from tests.test_media import (
    jpeg_with_exif_datetime,
    minimal_mp4_with_creation_date,
    minimal_png,
    minimal_tiff_with_datetime,
)


class StaticBrowserCliTests(unittest.TestCase):
    def test_manual_between_date_uses_midpoint_in_static_browser(self) -> None:
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
                    "--between",
                    "2004-06-01",
                    "2004-08-31",
                ]
            )
            self.assertEqual(code, 0, stderr)

            output = root / "index.html"
            self.assertEqual(run_cli(["--target", str(target), "make-browser", "--output", str(output)]), 0)
            html = output.read_text(encoding="utf-8")

        self.assertIn('"monthKey": "2004-07"', html)
        self.assertIn('"browserDate": "2004-07-16"', html)
        self.assertIn('"manualDateFrom": "2004-06-01"', html)
        self.assertIn('"manualDateTo": "2004-08-31"', html)

    def test_make_browser_uses_run_server_all_browser_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "PXL_20250102_123.MP").write_bytes(minimal_mp4_with_creation_date(dt.date(2025, 1, 2)))
            (source / "PXL_20250102_123.MP.jpg").write_bytes(b"image")
            (source / "DSC_0170.JPG").write_bytes(jpeg_with_exif_datetime("2019:03:03 12:00:00"))
            (source / "DSC_0170.NEF").write_bytes(minimal_tiff_with_datetime("2019:03:03 12:00:00"))
            (source / "deleted.jpg").write_bytes(jpeg_with_exif_datetime("2020:01:01 12:00:00"))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            conn = db.connect(target)
            try:
                conn.execute("UPDATE files SET deleted_at = CURRENT_TIMESTAMP WHERE stored_filename = 'deleted.jpg'")
                conn.commit()
            finally:
                conn.close()

            output = root / "index.html"
            self.assertEqual(run_cli(["--target", str(target), "make-browser", "--output", str(output)]), 0)
            html = output.read_text(encoding="utf-8")
            items_start = html.index("const embeddedItems = ") + len("const embeddedItems = ")
            items_end = html.index(";\n    const MONTH_PREVIEW_LIMIT", items_start)
            items = json.loads(html[items_start:items_end])
            item_ids = [int(item["fileId"]) for item in items]
            server_item_ids = source_item_ids(target, all_browser_source())

            self.assertEqual(item_ids, server_item_ids)
            self.assertEqual([item["name"] for item in items], ["DSC_0170.JPG", "PXL_20250102_123.MP.jpg"])
            self.assertNotIn("DSC_0170.NEF", html)
            self.assertNotIn("PXL_20250102_123.mp4", html)
            self.assertNotIn("deleted.jpg", html)

    def test_make_browser_can_hide_out_of_focus_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(jpeg_with_exif_datetime("2024:01:02 12:00:00"))
            (source / "IMG_20240103.jpg").write_bytes(jpeg_with_exif_datetime("2024:01:03 12:00:00"))

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            conn = db.connect(target)
            try:
                db.tag_file(conn, file_id=1, tag_name=db.SYSTEM_TAG_OUT_OF_FOCUS)
                conn.commit()
            finally:
                conn.close()

            output = root / "index.html"
            self.assertEqual(
                run_cli(["--target", str(target), "make-browser", "--hide-out-of-focus", "--output", str(output)]),
                0,
            )
            html = output.read_text(encoding="utf-8")
            items_start = html.index("const embeddedItems = ") + len("const embeddedItems = ")
            items_end = html.index(";\n    const MONTH_PREVIEW_LIMIT", items_start)
            items = json.loads(html[items_start:items_end])

            self.assertEqual(
                [int(item["fileId"]) for item in items],
                source_item_ids(target, all_browser_source(), hide_out_of_focus=True),
            )
            self.assertEqual([item["name"] for item in items], ["IMG_20240103.jpg"])
            self.assertNotIn("IMG_20240102.jpg", html)

    def test_make_browser_requires_target_lock_before_writing_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / LOCK_FILENAME).write_text("command=remove\n", encoding="utf-8")

            with (
                patch("bildebank.cli.recover_pending_file_moves"),
                patch("bildebank.cli.db.connect", side_effect=AssertionError("db before lock")),
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "make-browser"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Bildesamlingen er låst", stderr)
            self.assertFalse((target / "index.html").exists())

    def test_server_month_thumbnails_clip_rotated_images(self) -> None:
        self.assertIn(".thumb-link {", SERVER_CSS)
        self.assertIn("aspect-ratio: 4 / 3;", SERVER_CSS)
        self.assertIn("overflow: hidden;", SERVER_CSS)
        self.assertIn(".server-browser.month-browser", SERVER_CSS)
        self.assertIn("min-height: 100vh;", SERVER_CSS)
        self.assertIn("min-height: 100dvh;", SERVER_CSS)
        self.assertIn("grid-template-rows: max-content minmax(0, 1fr);", SERVER_CSS)
        self.assertIn(".years-grid-server", SERVER_CSS)
        self.assertIn(".years-browser .years-grid-server {\n      grid-template-columns: repeat(6, minmax(120px, 1fr));", SERVER_CSS)
        self.assertIn(".years-browser .years-grid-server .item", SERVER_CSS)
        self.assertIn("grid-template-rows: auto max-content;", SERVER_CSS)
        self.assertIn(".years-browser .years-grid-server .text", SERVER_CSS)
        self.assertIn("min-height: calc(2 * 13px * 1.2 + 12px);", SERVER_CSS)
        self.assertIn("padding: 6px 8px;", SERVER_CSS)
        self.assertIn(".years-browser .years-grid-server .path,\n    .years-browser .years-grid-server .score", SERVER_CSS)
        self.assertIn(".year-month-grid-server", SERVER_CSS)
        self.assertIn("grid-template-columns: repeat(6, minmax(120px, 1fr));", SERVER_CSS)
        self.assertIn("gap: 10px;", SERVER_CSS)
        self.assertIn(".year-month-grid-server .text { padding: 6px 8px; font-size: 13px; line-height: 1.2; }", SERVER_CSS)
        self.assertIn(".years-browser .years-grid-server { grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); }", SERVER_CSS)
        self.assertIn("grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));", SERVER_CSS)
        self.assertIn(".month-browser .month-grid-server { overflow: visible; }", SERVER_CSS)

    def test_static_browser_sorts_by_taken_date_inside_month(self) -> None:
        html = render_html([], month_preview_limit=None)
        compare = html[html.index("function compareItems") : html.index("function buildMonths")]

        month_order = compare.index("a.monthKey.localeCompare")
        date_order = compare.index("aDate.localeCompare")
        path_order = compare.index("a.path.localeCompare")

        self.assertLess(month_order, date_order)
        self.assertLess(date_order, path_order)

    def test_static_browser_has_dynamic_year_breadcrumb_and_overviews(self) -> None:
        html = render_html(
            [
                {
                    "path": "2022/01/<ferie & vinter>.jpg",
                    "url": "2022/01/%3Cferie%20%26%20vinter%3E.jpg",
                    "thumbnailSrc": "",
                    "kind": "image",
                    "monthKey": "2022-01",
                    "browserDate": "2022-01-02",
                    "name": "<ferie & vinter>.jpg",
                    "sizeText": "1 byte",
                },
                {
                    "path": "udatert/gammelt.jpg",
                    "url": "udatert/gammelt.jpg",
                    "thumbnailSrc": "",
                    "kind": "image",
                    "monthKey": "udatert",
                    "browserDate": "9999-99-99",
                    "name": "gammelt.jpg",
                    "sizeText": "1 byte",
                },
            ]
        )

        self.assertIn('<nav class="breadcrumb" aria-label="Plassering"><span>År</span></nav>', html)
        self.assertIn('"01": "Januar"', html)
        self.assertIn('"12": "Desember"', html)
        self.assertIn("function renderYears()", html)
        self.assertIn("function renderYear()", html)
        self.assertIn('state.viewMode = "years";', html)
        self.assertIn(
            'state.viewMode = year.key === "udatert" || year.key === "ukjent" ? "month" : "year";',
            html,
        )
        self.assertIn('parts.push({ label: "År", action: showYears });', html)
        self.assertIn(
            'action: state.viewMode === "item" ? event => showMonth(month.key, event) : null',
            html,
        )
        self.assertIn('if (state.viewMode === "item" && item) parts.push({ label: item.name });', html)
        self.assertIn('link.addEventListener("click", part.action);', html)
        self.assertIn("text.textContent = part.label;", html)
        self.assertIn("function attachSwipeNavigation", html)
        self.assertIn("window.PointerEvent", html)
        self.assertIn('event.pointerType !== "touch" && event.pointerType !== "pen"', html)
        self.assertIn('container.addEventListener("touchstart"', html)
        self.assertIn("moveItem(direction);", html)
        self.assertNotIn("<ferie & vinter>.jpg", html)
        self.assertIn(r"\u003cferie \u0026 vinter\u003e.jpg", html)
        self.assertNotIn("data-open-info", html)
        self.assertNotIn("<footer", html)
        self.assertNotIn('id="filename"', html)
        self.assertNotIn("filenameEl", html)
        self.assertNotIn("Årsoversikt:", html)
        self.assertNotIn("Månedsoversikt:", html)

    def test_make_browser_writes_index_with_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG 20240102.jpg").write_bytes(b"image-one")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)
            conn = sqlite3.connect(target / DB_FILENAME)
            try:
                file_id = int(conn.execute("SELECT id FROM files").fetchone()[0])
                conn.execute(
                    "UPDATE files SET view_rotation_degrees = 90 WHERE id = ?",
                    (file_id,),
                )
                conn.commit()
            finally:
                conn.close()
            face_conn = connect_face_db(target)
            try:
                face_conn.execute("INSERT INTO persons(id, name) VALUES(1, 'Kari')")
                face_conn.execute("INSERT INTO persons(id, name) VALUES(2, 'Ola Nordmann')")
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(1, ?, 'key', 1, 2, 10, 20, 0.9, 'test', ?)
                    """,
                    (file_id, b"embedding-1"),
                )
                face_conn.execute(
                    """
                    INSERT INTO faces(
                        id, file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                        detection_score, embedding_model, embedding
                    )
                    VALUES(2, ?, 'key', 3, 4, 12, 22, 0.8, 'test', ?)
                    """,
                    (file_id, b"embedding-2"),
                )
                face_conn.execute("INSERT INTO person_faces(person_id, face_id) VALUES(1, 1)")
                face_conn.execute("INSERT INTO face_suggestions(person_id, face_id, similarity) VALUES(2, 2, 0.91)")
                face_conn.commit()
            finally:
                face_conn.close()

            code, stdout, stderr = capture_cli(["--target", str(target), "make-browser"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev HTML-browser", stdout)
            html = (target / "index.html").read_text(encoding="utf-8")
            self.assertIn('"path": "2024/01/IMG 20240102.jpg"', html)
            self.assertIn('"url": "2024/01/IMG%2020240102.jpg"', html)
            self.assertIn('"sizeText": "9 bytes"', html)
            self.assertIn('"viewRotation": 90', html)
            self.assertIn('applyImageViewRotation(img, item, "contain");', html)
            self.assertIn('applyImageViewRotation(img, item, "cover");', html)
            self.assertNotIn("padding: 10px;\n      overflow: hidden;", html)
            self.assertNotIn("border-radius: 8px;\n      overflow: hidden;\n    }\n    .media-link", html)
            self.assertIn("html, body {\n      width: 100%;\n      height: 100%;\n      overflow: hidden;", html)
            self.assertIn("height: 100dvh;", html)
            self.assertIn("width: auto;\n      height: auto;\n      max-width: 100%;\n      max-height: 100%;", html)
            self.assertNotIn("max-height: calc(100vh - 10rem);", html)
            self.assertIn("const MONTH_PREVIEW_LIMIT = null;", html)
            self.assertIn('state.viewMode = "month";', html)
            self.assertIn("function representativeItems(items, limit)", html)
            self.assertIn('id="title" class="title"', html)
            self.assertIn("function renderBreadcrumb()", html)
            self.assertIn("function renderYears()", html)
            self.assertIn('"01": "Januar"', html)
            self.assertIn('<nav class="controls" aria-label="Navigering">', html)
            self.assertIn('data-nav-button-pair="year"', html)
            self.assertIn('title="Forrige år">◀ Å</button>', html)
            self.assertIn('title="Neste år">r ▶</button>', html)
            self.assertIn('data-nav-button-pair="month"', html)
            self.assertIn('title="Forrige måned">◀ Mån</button>', html)
            self.assertIn('title="Neste måned">ed ▶</button>', html)
            self.assertIn('data-nav-button-pair="item"', html)
            self.assertIn('title="Forrige bilde">◀ Bil</button>', html)
            self.assertIn('title="Neste bilde">de ▶</button>', html)
            self.assertNotIn('id="position"', html)
            self.assertNotIn("positionEl", html)
            self.assertNotIn("server-search-link", html)
            self.assertIn('img.loading = "lazy";', html)
            self.assertNotIn('"people":', html)
            self.assertNotIn('"faces":', html)
            self.assertNotIn("Personer:", html)
            self.assertNotIn("(forslag)", html)
            self.assertNotIn("Ansikter i bildet", html)
            self.assertNotIn('face-person-add-face "Navn"', html)
            self.assertNotIn("navigator.clipboard.writeText", html)
            self.assertNotIn("fallbackCopyCommand", html)

            limited_output = root / "limited.html"
            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "make-browser",
                    "--month-preview-limit",
                    "40",
                    "--output",
                    str(limited_output),
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev HTML-browser", stdout)
            limited_html = limited_output.read_text(encoding="utf-8")
            self.assertIn("const MONTH_PREVIEW_LIMIT = 40;", limited_html)
            self.assertIn("if (limit === 1) return [items[0]];", limited_html)

    def test_make_browser_writes_custom_output_without_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            source = root / "source"
            source.mkdir()
            (source / "IMG_20240102.jpg").write_bytes(b"image-one")
            (source / "video.mp4").write_bytes(minimal_mp4_with_creation_date(dt.date(2010, 7, 8)))
            (source / "IMG_20240103.nef").write_bytes(b"raw-photo")

            self.assertEqual(run_cli(["create", str(target)]), 0)
            self.assertEqual(run_cli(["--target", str(target), "import", "--name", source.name, "--quiet", str(source)]), 0)

            custom_output = root / "filtered.html"
            code, stdout, stderr = capture_cli(
                [
                    "--target",
                    str(target),
                    "make-browser",
                    "--output",
                    str(custom_output),
                ]
            )

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev HTML-browser", stdout)
            self.assertTrue(custom_output.exists())
            html = custom_output.read_text(encoding="utf-8")
            self.assertIn('"path": "2010/07/video.mp4"', html)
            self.assertIn('"path": "2024/01/IMG_20240102.jpg"', html)
            self.assertIn('"path": "2024/01/IMG_20240103.nef"', html)
            self.assertIn('"kind": "file"', html)
            self.assertIn('item.kind === "image"', html)

    def test_static_browser_normalizes_all_view_rotations_and_leaves_video_unrotated(self) -> None:
        html = render_html(
            [
                {
                    "path": f"2024/01/{rotation}.jpg",
                    "url": f"{rotation}.jpg",
                    "thumbnailSrc": f"{rotation}.jpg",
                    "kind": "image",
                    "viewRotation": rotation,
                    "monthKey": "2024-01",
                    "name": f"{rotation}.jpg",
                    "sizeText": "1 byte",
                }
                for rotation in (0, 90, 180, 270)
            ]
            + [
                {
                    "path": "2024/01/video.mp4",
                    "url": "video.mp4",
                    "thumbnailSrc": "",
                    "kind": "video",
                    "viewRotation": 90,
                    "monthKey": "2024-01",
                    "name": "video.mp4",
                    "sizeText": "1 byte",
                }
            ]
        )

        for rotation in (0, 90, 180, 270):
            self.assertIn(f'"viewRotation": {rotation}', html)
        self.assertIn("const quarterTurn = rotation === 90 || rotation === 270;", html)
        self.assertIn('fit === "cover" ? Math.max(scaleX, scaleY) : Math.min(scaleX, scaleY)', html)
        self.assertIn('container.classList.add("view-rotation-container");', html)
        self.assertIn('translate(-50%, -50%) rotate(${rotation}deg)', html)
        self.assertLess(
            html.index("link.append(img);"),
            html.index('applyImageViewRotation(img, item, "contain");'),
        )
        self.assertLess(
            html.index("button.append(img);"),
            html.index('applyImageViewRotation(img, item, "cover");'),
        )
        self.assertIn('if (item.kind === "video")', html)
        self.assertIn('} else if (item.kind === "image")', html)

    def test_make_browser_help_omits_filters(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["make-browser", "-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("make-browser", stdout)
        self.assertIn("--month-preview-limit", stdout)
        self.assertIn("--output", stdout)
        self.assertNotIn("--media", stdout)
        self.assertNotIn("--date-source", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_thumbnail_paths_and_existing_url_use_current_thumbnail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            original = target / "2024" / "01" / "image.png"
            write_test_image(original)

            relative = Path("2024/01/image.png")
            thumb_relative = thumbnail_relative_path(relative)
            thumb_path = thumbnail_absolute_path(target, relative)

            self.assertEqual(thumb_relative, Path("thumbs/2024/01/image.jpg"))
            self.assertEqual(existing_thumbnail_url(target, relative), "2024/01/image.png")

            write_test_image(thumb_path)
            os.utime(thumb_path, ns=(original.stat().st_mtime_ns + 1_000_000, original.stat().st_mtime_ns + 1_000_000))

            self.assertTrue(thumbnail_is_current(original, thumb_path))
            self.assertEqual(existing_thumbnail_url(target, relative), "thumbs/2024/01/image.jpg")

    def test_existing_thumbnail_url_falls_back_when_thumbnail_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            original = target / "2024" / "01" / "image.jpg"
            write_test_image(original)
            relative = Path("2024/01/image.jpg")
            thumb_path = thumbnail_absolute_path(target, relative)
            write_test_image(thumb_path)
            os.utime(thumb_path, ns=(original.stat().st_mtime_ns - 1_000_000, original.stat().st_mtime_ns - 1_000_000))

            self.assertFalse(thumbnail_is_current(original, thumb_path))
            self.assertEqual(existing_thumbnail_url(target, relative), "2024/01/image.jpg")

    def test_make_thumbnails_continues_after_corrupt_file_and_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            good = target / "2024" / "01" / "good.jpg"
            bad = target / "2024" / "01" / "bad.jpg"
            write_test_image(good)
            bad.write_bytes(b"not a real image")
            register_target_file(target, Path("2024/01/good.jpg"))
            register_target_file(target, Path("2024/01/bad.jpg"))

            code, stdout, stderr = capture_cli(["--target", str(target), "make-thumbnails"])

            self.assertEqual(code, 2, stderr)
            self.assertIn("feil=1", stdout)
            self.assertTrue(thumbnail_absolute_path(target, Path("2024/01/good.jpg")).is_file())

    def test_make_thumbnails_shows_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            image = target / "2024" / "01" / "image.jpg"
            write_test_image(image)
            register_target_file(target, Path("2024/01/image.jpg"))

            code, stdout, stderr = capture_cli(["--target", str(target), "make-thumbnails"])

        self.assertEqual(code, 0, stderr)
        self.assertIn("Thumbnails: 1 filer skal kontrolleres.", stdout)
        self.assertIn("Thumbnails: kontrollert=1/1", stdout)
        self.assertIn("Thumbnails: ferdig kontrollert 1/1 filer.", stdout)

    def test_make_thumbnails_takes_target_lock_before_database_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            self.assertEqual(run_cli(["create", str(target)]), 0)
            (target / LOCK_FILENAME).write_text("command=remove\n", encoding="utf-8")

            with (
                patch("bildebank.cli.recover_pending_file_moves"),
                patch("bildebank.cli.db.connect", side_effect=AssertionError("db before lock")),
            ):
                code, stdout, stderr = capture_cli(["--target", str(target), "make-thumbnails"])

            self.assertEqual(code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Bildesamlingen er låst", stderr)

    def test_make_browser_writes_thumbnail_src_when_thumbnail_is_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            original = target / "2024" / "01" / "image.jpg"
            write_test_image(original)
            register_target_file(target, Path("2024/01/image.jpg"))
            thumb_path = thumbnail_absolute_path(target, Path("2024/01/image.jpg"))
            write_test_image(thumb_path)
            os.utime(thumb_path, ns=(original.stat().st_mtime_ns + 1_000_000, original.stat().st_mtime_ns + 1_000_000))

            code, stdout, stderr = capture_cli(["--target", str(target), "make-browser"])

            self.assertEqual(code, 0, stderr)
            self.assertIn("Skrev HTML-browser", stdout)
            html = (target / "index.html").read_text(encoding="utf-8")
            self.assertIn('"thumbnailSrc": "thumbs/2024/01/image.jpg"', html)
            self.assertIn("item.thumbnailSrc || item.url", html)

    def test_server_month_uses_current_thumbnail_via_file_thumbs_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            original = target / "2024" / "01" / "image.jpg"
            write_test_image(original)
            register_target_file(target, Path("2024/01/image.jpg"))
            thumb_path = thumbnail_absolute_path(target, Path("2024/01/image.jpg"))
            write_test_image(thumb_path)
            os.utime(thumb_path, ns=(original.stat().st_mtime_ns + 1_000_000, original.stat().st_mtime_ns + 1_000_000))

            items = browser_month_items(target, "2024-01")
            html = month_page_html(target, "2024-01", items)

            self.assertIn('src="/file/thumbs/2024/01/image.jpg"', html)

    def test_docs_reference_includes_make_thumbnails(self) -> None:
        reference = Path("docs/reference.md").read_text(encoding="utf-8")

        self.assertIn("[`make-thumbnails`](make-thumbnails.md)", reference)
