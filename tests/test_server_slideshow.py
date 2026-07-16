from __future__ import annotations

import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from http import HTTPStatus
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bildebank import db
from bildebank.cli import (
    build_parser,
    main,
    run_target_command,
    should_recover_pending_file_moves,
)
from bildebank.config import AppConfig
from bildebank.server_handler import BildebankRequestHandler
from bildebank.server_runtime import BildebankServer
from bildebank.server_slideshow import (
    DEFAULT_SLIDESHOW_DELAY_SECONDS,
    Slideshow,
    SlideshowItem,
    build_slideshow,
    slideshow_html,
)
from tests.cli_helpers import write_test_image
from tests.db_test_helpers import insert_test_file, register_target_file


class ServerSlideshowTests(unittest.TestCase):
    def test_cli_parses_slideshow_options_and_documents_them(self) -> None:
        args = build_parser().parse_args(
            [
                "run-server",
                "--slideshow",
                "--delay",
                "7",
                "--filter",
                "year=1999",
            ]
        )

        self.assertTrue(args.slideshow)
        self.assertEqual(args.delay, 7)
        self.assertEqual(args.slideshow_filter, "year=1999")
        self.assertFalse(should_recover_pending_file_moves(args))

        stdout = StringIO()
        with patch("sys.stdout", stdout), self.assertRaises(SystemExit) as raised:
            main(["run-server", "-h"])

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("--slideshow", stdout.getvalue())
        self.assertIn("--delay SEKUNDER", stdout.getvalue())
        self.assertIn("--filter UTTRYKK", stdout.getvalue())

    def test_cli_slideshow_implies_lan_read_only_preview_and_default_delay(self) -> None:
        args = build_parser().parse_args(
            ["run-server", "--slideshow", "--filter", "year=1999"]
        )
        with patch("bildebank.cli.run_server_command", return_value=0) as run_server:
            result = run_target_command(args, Path("C:/Users/Tom/Bilder"))

        self.assertEqual(result, 0)
        run_server.assert_called_once_with(
            Path("C:/Users/Tom/Bilder"),
            host="0.0.0.0",
            port=8765,
            repo_root=run_server.call_args.kwargs["repo_root"],
            browser=True,
            allow_remote=True,
            preview_images=True,
            read_only=True,
            lan_share=True,
            slideshow_delay_seconds=DEFAULT_SLIDESHOW_DELAY_SECONDS,
            slideshow_filter="year=1999",
        )

    def test_cli_rejects_slideshow_host_and_slideshow_only_options(self) -> None:
        cases = [
            (["run-server", "--slideshow", "--host", "0.0.0.0"], "--slideshow"),
            (["run-server", "--filter", "year=1999"], "--filter"),
            (["run-server", "--delay", "4"], "--delay"),
        ]
        for argv, message in cases:
            with self.subTest(argv=argv):
                stderr = StringIO()
                with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                    main(argv)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn(message, stderr.getvalue())

    def test_build_slideshow_uses_filter_order_and_only_active_still_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            db.init_database(target)
            second_id = insert_test_file(target, "2024/01/b.png")
            first_id = insert_test_file(target, "2024/01/a.png")
            deleted_id = insert_test_file(
                target, "deleted/2024/01/deleted.png", deleted=True
            )
            video_id = insert_test_file(target, "2024/01/video.mp4")
            missing_id = insert_test_file(target, "2024/01/missing.png")
            (target / "2024/01/missing.png").unlink()
            conn = db.connect(target)
            try:
                conn.execute(
                    "UPDATE files SET view_rotation_degrees = 90 WHERE id = ?",
                    (first_id,),
                )
                conn.commit()
            finally:
                conn.close()

            slideshow = build_slideshow(
                target,
                AppConfig(),
                filter_query="year=2024",
                delay_seconds=10,
            )

        self.assertEqual(
            [item.file_id for item in slideshow.items],
            [first_id, second_id],
        )
        self.assertEqual(slideshow.items[0].view_rotation_degrees, 90)
        self.assertNotIn(deleted_id, slideshow.item_ids)
        self.assertNotIn(video_id, slideshow.item_ids)
        self.assertNotIn(missing_id, slideshow.item_ids)

    def test_source_filter_does_not_duplicate_file_with_multiple_provenance_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            db.init_database(target)
            file_id = insert_test_file(target, "2024/01/image.png", sha256="same")
            image_path = target / "2024/01/image.png"
            conn = db.connect(target)
            try:
                first_source_id = db.add_named_source(
                    conn, target / "source-one", "Mobil"
                )
                second_source_id = db.add_named_source(
                    conn, target / "source-two", "Mobil backup"
                )
                for source_id, source_name in (
                    (first_source_id, "one.png"),
                    (second_source_id, "two.png"),
                ):
                    db.insert_or_validate_file_source(
                        conn,
                        file_id=file_id,
                        source_id=source_id,
                        source_path=str(target / source_name),
                        source_path_key=str(target / source_name).casefold(),
                        sha256="same",
                        size_bytes=image_path.stat().st_size,
                    )
                conn.commit()
            finally:
                conn.close()

            slideshow = build_slideshow(
                target,
                AppConfig(),
                filter_query="source:Mobil",
                delay_seconds=10,
            )

        self.assertEqual([item.file_id for item in slideshow.items], [file_id])

    def test_build_slideshow_rejects_invalid_and_empty_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            db.init_database(target)
            insert_test_file(target, "2024/01/image.png")

            with self.assertRaisesRegex(ValueError, "Ukjent filter"):
                build_slideshow(
                    target,
                    AppConfig(),
                    filter_query="unknown:value",
                    delay_seconds=10,
                )
            with self.assertRaisesRegex(ValueError, "ingen aktive stillbilder"):
                build_slideshow(
                    target,
                    AppConfig(),
                    filter_query="year=1999",
                    delay_seconds=10,
                )

    def test_slideshow_html_is_minimal_starts_first_and_shows_whole_image(self) -> None:
        slideshow = Slideshow(
            items=(
                SlideshowItem(4, 90),
                SlideshowItem(8, 0),
            ),
            item_ids=frozenset({4, 8}),
            delay_seconds=10,
        )

        body = slideshow_html(slideshow)

        self.assertIn('object-fit: contain', body)
        self.assertIn('background: #000', body)
        self.assertIn('show(0);', body)
        self.assertIn('const delayMs = 10000;', body)
        self.assertIn('new Image()', body)
        self.assertIn('loader.onload', body)
        self.assertIn('window.setTimeout', body)
        self.assertIn('"rotation":90', body)
        self.assertNotIn("<a ", body)
        self.assertNotIn("<button", body)

    def test_slideshow_mode_allows_only_root_and_member_media(self) -> None:
        slideshow = Slideshow(
            items=(SlideshowItem(4, 0),),
            item_ids=frozenset({4}),
            delay_seconds=10,
        )
        handler = object.__new__(BildebankRequestHandler)
        handler.server = SimpleNamespace(slideshow=slideshow, read_only=True)
        handler.respond_html = Mock()
        handler.respond_text = Mock()
        handler.respond_preview_image = Mock(return_value=True)

        handler.path = "/"
        BildebankRequestHandler.do_GET(handler)
        handler.respond_html.assert_called_once()

        handler.path = "/slideshow/media/4"
        BildebankRequestHandler.do_GET(handler)
        handler.respond_preview_image.assert_called_once_with(4, require_active=True)

        for path in ("/slideshow/media/8", "/item/4", "/file/4", "/settings"):
            with self.subTest(path=path):
                handler.respond_text.reset_mock()
                handler.path = path
                BildebankRequestHandler.do_GET(handler)
                self.assertEqual(
                    handler.respond_text.call_args.kwargs["status"],
                    HTTPStatus.NOT_FOUND,
                )

        handler.respond_text.reset_mock()
        handler.path = "/api/item-tag"
        BildebankRequestHandler.do_POST(handler)
        self.assertEqual(
            handler.respond_text.call_args.kwargs["status"],
            HTTPStatus.NOT_FOUND,
        )

    def test_slideshow_http_smoke_serves_page_preview_and_no_browser_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            db.init_database(target)
            relative_path = Path("2024/01/image.png")
            write_test_image(target / relative_path)
            file_id = register_target_file(target, relative_path)
            slideshow = build_slideshow(
                target,
                AppConfig(),
                filter_query="year=2024",
                delay_seconds=10,
            )
            try:
                server = BildebankServer(
                    ("127.0.0.1", 0),
                    target,
                    AppConfig(),
                    preview_images=True,
                    read_only=True,
                    slideshow=slideshow,
                )
            except PermissionError as exc:
                self.skipTest(f"Miljøet tillater ikke lokal HTTP-socket: {exc}")
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            port = server.server_address[1]
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/", timeout=5
                ) as response:
                    self.assertEqual(response.status, HTTPStatus.OK)
                    self.assertIn(b'id="slideshow"', response.read())
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/slideshow/media/{file_id}",
                    timeout=5,
                ) as response:
                    self.assertEqual(response.status, HTTPStatus.OK)
                    self.assertEqual(response.headers.get_content_type(), "image/jpeg")
                    self.assertTrue(response.read())
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/item/{file_id}", timeout=5
                    )
                self.assertEqual(raised.exception.code, HTTPStatus.NOT_FOUND)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
