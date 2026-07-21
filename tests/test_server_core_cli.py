from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http import HTTPStatus
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bildebank import db
from bildebank.cli import build_parser, main, should_recover_pending_file_moves
from bildebank.cli_server import lan_share_urls, run_server_command
from bildebank.config import AppConfig, FaceRecognitionConfig, OpenClipConfig
from bildebank.db import init_database
from bildebank.server_handler import (
    BildebankRequestHandler,
    resolve_doc_asset_path,
)
from bildebank.server_runtime import (
    BildebankServer,
    is_local_bind_host,
    run_server as run_http_server,
    validate_bind_host,
)
from bildebank.server_assets import SERVER_JS
from bildebank.server_browser_item_html import item_media_html
from bildebank.target_lock import LOCK_FILENAME
from bildebank.server_files import server_file_path_by_id
from bildebank.server_pages import (
    app_status_page_html,
    index_html,
    markdown_doc_page_html,
    search_html,
    sources_page_html,
)
from bildebank.server_response import add_csrf_to_html
from bildebank.server_search import DEFAULT_SEARCH_LIMIT, ServerSearchStats
from tests.cli_helpers import write_test_image
from tests.db_test_helpers import register_target_file


class ServerCoreCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.program_root_tempdir = tempfile.TemporaryDirectory()
        self.program_root = Path(self.program_root_tempdir.name)
        self.program_root_patcher = patch("bildebank.cli.program_repo_root", return_value=self.program_root)
        self.program_root_patcher.start()

    def tearDown(self) -> None:
        self.program_root_patcher.stop()
        self.program_root_tempdir.cleanup()

    def test_run_server_help_documents_local_options(self) -> None:
        stdout_buffer = StringIO()
        stderr_buffer = StringIO()
        with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer), self.assertRaises(SystemExit) as raised:
            main(["run-server", "-h"])

        self.assertEqual(raised.exception.code, 0)
        stdout = stdout_buffer.getvalue()
        self.assertIn("usage: bildebank run-server [valg]", stdout)
        self.assertIn("--host", stdout)
        self.assertIn("--port", stdout)
        self.assertIn("--no-browser", stdout)
        self.assertIn("--preview-images", stdout)
        self.assertIn("--read-only", stdout)
        self.assertIn("--lan-share", stdout)
        self.assertIn("--allow-remote", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_run_server_preview_images_is_explicit_and_defaults_to_false(self) -> None:
        default_args = build_parser().parse_args(["run-server"])
        preview_args = build_parser().parse_args(["run-server", "--preview-images"])

        self.assertFalse(default_args.preview_images)
        self.assertTrue(preview_args.preview_images)

    def test_run_server_read_only_is_explicit_and_defaults_to_false(self) -> None:
        default_args = build_parser().parse_args(["run-server"])
        read_only_args = build_parser().parse_args(["run-server", "--read-only"])

        self.assertFalse(default_args.read_only)
        self.assertTrue(read_only_args.read_only)

    def test_read_only_server_skips_pending_file_move_recovery(self) -> None:
        args = build_parser().parse_args(["run-server", "--read-only"])
        self.assertFalse(should_recover_pending_file_moves(args))

        lan_share_args = build_parser().parse_args(["run-server", "--lan-share"])
        self.assertFalse(should_recover_pending_file_moves(lan_share_args))

    def test_read_only_server_can_start_while_target_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            (target / LOCK_FILENAME).write_text("command=face-scan\n", encoding="utf-8")

            with patch("bildebank.cli.run_server_command", return_value=0) as run_server:
                code = main(["--target", str(target), "run-server", "--read-only", "--no-browser"])

        self.assertEqual(code, 0)
        run_server.assert_called_once()

    def test_read_only_item_media_does_not_fill_missing_metadata_cache(self) -> None:
        item = {
            "id": 1,
            "target_path": "2024/01/IMG_20240102.jpg",
            "stored_filename": "IMG_20240102.jpg",
            "view_rotation_degrees": 90,
            "media_width": None,
            "media_height": None,
        }
        with patch(
            "bildebank.server_browser_item_html.cached_image_dimensions",
            side_effect=AssertionError("read-only skal ikke skrive metadata-cache"),
        ):
            body = item_media_html(Path("target"), item, read_only=True)

        self.assertIn('src="/preview/1"', body)

    def test_run_server_lan_share_is_explicit_and_rejects_host(self) -> None:
        args = build_parser().parse_args(["run-server", "--lan-share", "--port", "8766"])
        self.assertTrue(args.lan_share)
        self.assertIsNone(args.host)
        self.assertEqual(args.port, 8766)

        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(["run-server", "--lan-share", "--host", "0.0.0.0"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--lan-share kan ikke brukes sammen med --host", stderr.getvalue())

    def test_lan_share_urls_use_private_ipv4_addresses(self) -> None:
        with patch("bildebank.cli_server.local_lan_ipv4_addresses", return_value=["192.168.86.11"]):
            self.assertEqual(lan_share_urls(8766), ["http://192.168.86.11:8766/"])

    def test_run_server_local_bind_host_detection(self) -> None:
        cases = {
            "127.0.0.1": True,
            "localhost": True,
            "::1": True,
            "0.0.0.0": False,
            "::": False,
            "": False,
            "192.168.1.10": False,
            "10.0.0.5": False,
            "my-pc": False,
        }

        for host, expected in cases.items():
            with self.subTest(host=host):
                self.assertEqual(is_local_bind_host(host), expected)

    def test_run_server_bind_host_requires_explicit_remote_permission(self) -> None:
        with self.assertRaisesRegex(ValueError, "--allow-remote"):
            validate_bind_host("192.168.1.10", allow_remote=False)

        validate_bind_host("192.168.1.10", allow_remote=True)
        validate_bind_host("127.0.0.1", allow_remote=False)

        args = build_parser().parse_args(
            ["run-server", "--host", "0.0.0.0", "--allow-remote", "--no-browser"]
        )
        self.assertTrue(args.allow_remote)
        self.assertEqual(args.host, "0.0.0.0")

    def test_run_server_command_forwards_remote_permission(self) -> None:
        config = AppConfig()
        with (
            patch("bildebank.cli_server.load_config", return_value=config),
            patch("bildebank.cli_server.lan_share_urls", return_value=["http://192.168.86.11:8765/"]),
            patch("bildebank.cli_server.run_local_server") as run_local_server,
            redirect_stdout(StringIO()) as stdout,
        ):
            run_local_server.side_effect = lambda *args, **kwargs: kwargs["ready"]("http://0.0.0.0:8765/")
            result = run_server_command(
                Path("C:/Users/Tom/Bilder"),
                host="0.0.0.0",
                port=8765,
                repo_root=self.program_root,
                browser=False,
                allow_remote=True,
                preview_images=True,
                read_only=True,
                lan_share=True,
            )

        self.assertEqual(result, 0)
        run_local_server.assert_called_once()
        self.assertEqual(run_local_server.call_args.kwargs["host"], "0.0.0.0")
        self.assertTrue(run_local_server.call_args.kwargs["allow_remote"])
        self.assertTrue(run_local_server.call_args.kwargs["preview_images"])
        self.assertTrue(run_local_server.call_args.kwargs["read_only"])
        output = stdout.getvalue()
        self.assertIn("LAN-share er aktiv", output)
        self.assertIn("Serveren kan nås av alle på samme LAN", output)
        self.assertIn("Ikke bruk --lan-share på offentlige nettverk", output)
        self.assertIn("http://192.168.86.11:8765/", output)

    def test_run_server_lan_share_opens_localhost_in_browser(self) -> None:
        config = AppConfig()
        with (
            patch("bildebank.cli_server.load_config", return_value=config),
            patch("bildebank.cli_server.lan_share_urls", return_value=["http://192.168.86.11:8766/"]),
            patch("bildebank.cli_server.run_local_server") as run_local_server,
            patch("bildebank.cli_server.webbrowser.open") as open_browser,
            redirect_stdout(StringIO()),
        ):
            run_local_server.side_effect = lambda *args, **kwargs: kwargs["ready"]("http://0.0.0.0:8766/")
            result = run_server_command(
                Path("C:/Users/Tom/Bilder"),
                host="0.0.0.0",
                port=8766,
                repo_root=self.program_root,
                browser=True,
                allow_remote=True,
                preview_images=True,
                read_only=True,
                lan_share=True,
            )

        self.assertEqual(result, 0)
        open_browser.assert_called_once_with("http://127.0.0.1:8766/")

    def test_run_server_warns_before_allowed_remote_bind(self) -> None:
        fake_server = SimpleNamespace(
            server_address=("0.0.0.0", 8765),
            serve_forever=lambda: None,
            server_close=lambda: None,
        )
        stderr = StringIO()
        with (
            patch("bildebank.server_runtime.db.prepare_database"),
            patch("bildebank.server_runtime.BildebankServer", return_value=fake_server) as server_class,
            redirect_stderr(stderr),
        ):
            run_http_server(
                Path("."),
                AppConfig(),
                host="0.0.0.0",
                allow_remote=True,
            )

        server_class.assert_called_once_with(
            ("0.0.0.0", 8765),
            Path("."),
            AppConfig(),
            preview_images=False,
            read_only=False,
            slideshow=None,
        )
        self.assertIn("ADVARSEL", stderr.getvalue())
        self.assertIn("andre maskiner på nettverket", stderr.getvalue())

    def test_run_server_creates_server_in_read_only_mode(self) -> None:
        fake_server = SimpleNamespace(
            server_address=("127.0.0.1", 8765),
            serve_forever=lambda: None,
            server_close=lambda: None,
        )
        with (
            patch("bildebank.server_runtime.db.prepare_database"),
            patch("bildebank.server_runtime.BildebankServer", return_value=fake_server) as server_class,
        ):
            run_http_server(Path("."), AppConfig(), read_only=True)

        server_class.assert_called_once_with(
            ("127.0.0.1", 8765),
            Path("."),
            AppConfig(),
            preview_images=False,
            read_only=True,
            slideshow=None,
        )

    def test_read_only_blocks_admin_gets_and_posts_before_csrf(self) -> None:
        handler = object.__new__(BildebankRequestHandler)
        handler.server = SimpleNamespace(read_only=True)
        handler.path = "/settings"
        handler.text_response = None
        handler.json_response = None
        handler.respond_text = lambda content, *, status=HTTPStatus.OK: setattr(
            handler, "text_response", (content, status)
        )
        handler.respond_json = lambda content, *, status=HTTPStatus.OK: setattr(
            handler, "json_response", (content, status)
        )

        BildebankRequestHandler.do_GET(handler)  # type: ignore[arg-type]
        self.assertEqual(handler.text_response[1], HTTPStatus.FORBIDDEN)

        handler.path = "/api/item-tag"
        BildebankRequestHandler.do_POST(handler)  # type: ignore[arg-type]
        self.assertEqual(handler.json_response[1], HTTPStatus.FORBIDDEN)
        self.assertIn("read-only", handler.json_response[0]["error"])

        handler.path = "/search?q=strand"
        handler.text_response = None
        BildebankRequestHandler.do_GET(handler)  # type: ignore[arg-type]
        self.assertEqual(handler.text_response[1], HTTPStatus.FORBIDDEN)

    def test_run_server_display_returns_full_size_rotated_jpeg_when_preview_images_is_false(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            image_path = target / "2024" / "01" / "image.png"
            write_test_image(image_path, size=(3000, 1000))
            file_id = register_target_file(target, Path("2024/01/image.png"))
            with db.connect(target) as conn:
                conn.execute(
                    "UPDATE files SET view_rotation_degrees = 90 WHERE id = ?",
                    (file_id,),
                )

            class FakeHandler:
                server = SimpleNamespace(target=target, preview_images=False)
                content = b""
                content_type = ""
                status = HTTPStatus.OK

                def respond_bytes(
                    self,
                    content: bytes,
                    content_type: str,
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    self.content = content
                    self.content_type = content_type
                    self.status = status

                def respond_text(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    raise AssertionError(f"{status}: {content}")

                def respond_preview_image(self, requested_file_id: int, **kwargs: object) -> None:
                    BildebankRequestHandler.respond_preview_image(  # type: ignore[arg-type]
                        self, requested_file_id, **kwargs
                    )

            handler = FakeHandler()
            BildebankRequestHandler.respond_display(handler, str(file_id))  # type: ignore[arg-type]
            with Image.open(BytesIO(handler.content)) as display:
                display_format = display.format
                display_size = display.size

        self.assertEqual(handler.content_type, "image/jpeg")
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(display_format, "JPEG")
        self.assertEqual(display_size, (1000, 3000))

    def test_run_server_preview_returns_original_when_preview_images_is_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            image_path = target / "2024" / "01" / "image.png"
            write_test_image(image_path)
            file_id = register_target_file(target, Path("2024/01/image.png"))
            original = image_path.read_bytes()

            class FakeHandler:
                server = SimpleNamespace(target=target, preview_images=False)
                content = b""
                content_type = ""
                status = HTTPStatus.OK

                def respond_bytes(
                    self,
                    content: bytes,
                    content_type: str,
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    self.content = content
                    self.content_type = content_type
                    self.status = status

                def respond_text(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    raise AssertionError(f"{status}: {content}")

                def respond_file(self, encoded_relative_path: str) -> None:
                    BildebankRequestHandler.respond_file(self, encoded_relative_path)  # type: ignore[arg-type]

            handler = FakeHandler()
            BildebankRequestHandler.respond_preview(handler, str(file_id))  # type: ignore[arg-type]

        self.assertEqual(handler.content, original)
        self.assertEqual(handler.content_type, "image/png")
        self.assertEqual(handler.status, HTTPStatus.OK)

    def test_run_server_display_returns_scaled_jpeg_when_preview_images_is_true(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            image_path = target / "2024" / "01" / "image.png"
            write_test_image(image_path, size=(3000, 1000))
            file_id = register_target_file(target, Path("2024/01/image.png"))

            class FakeHandler:
                server = SimpleNamespace(target=target, preview_images=True)
                content = b""
                content_type = ""
                status = HTTPStatus.OK

                def respond_bytes(
                    self,
                    content: bytes,
                    content_type: str,
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    self.content = content
                    self.content_type = content_type
                    self.status = status

                def respond_text(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    raise AssertionError(f"{status}: {content}")

                def respond_preview_image(self, requested_file_id: int, **kwargs: object) -> None:
                    BildebankRequestHandler.respond_preview_image(  # type: ignore[arg-type]
                        self, requested_file_id, **kwargs
                    )

            handler = FakeHandler()
            BildebankRequestHandler.respond_display(handler, str(file_id))  # type: ignore[arg-type]
            with Image.open(BytesIO(handler.content)) as preview:
                preview_format = preview.format
                preview_size = preview.size

        self.assertEqual(handler.content_type, "image/jpeg")
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(preview_format, "JPEG")
        self.assertEqual(preview_size, (1600, 533))

    def test_run_server_display_rejects_non_image_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            file_path = target / "2024" / "01" / "video.mp4"
            file_path.parent.mkdir(parents=True)
            file_path.write_bytes(b"not an image")
            file_id = register_target_file(target, Path("2024/01/video.mp4"))

            class FakeHandler:
                server = SimpleNamespace(target=target, preview_images=True)
                body = ""
                status = HTTPStatus.OK

                def respond_bytes(self, content: bytes, content_type: str, *, status=HTTPStatus.OK) -> None:
                    raise AssertionError("Non-image should not return preview bytes")

                def respond_text(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    self.body = content
                    self.status = status

                def respond_preview_image(self, requested_file_id: int, **kwargs: object) -> None:
                    BildebankRequestHandler.respond_preview_image(  # type: ignore[arg-type]
                        self, requested_file_id, **kwargs
                    )

            handler = FakeHandler()
            BildebankRequestHandler.respond_display(handler, str(file_id))  # type: ignore[arg-type]

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertIn("ikke et bilde", handler.body)

    def test_run_server_resolves_help_images_under_docs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "program"
            docs = root / "docs"
            image_path = docs / "screenshots" / "bildebank.png"
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"png")
            (docs / "not-image.txt").write_text("text", encoding="utf-8")

            with patch("bildebank.server_app.server_program_repo_root", return_value=root):
                self.assertEqual(resolve_doc_asset_path("screenshots/bildebank.png"), image_path.resolve())
                self.assertIsNone(resolve_doc_asset_path("../secret.png"))
                self.assertIsNone(resolve_doc_asset_path("/screenshots/bildebank.png"))
                self.assertIsNone(resolve_doc_asset_path("not-image.txt"))

    def test_run_server_responds_with_help_image_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "program"
            target = Path(tmp) / "target"
            image_path = root / "docs" / "screenshots" / "bildebank.png"
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"png-bytes")

            class FakeHandler:
                server = SimpleNamespace(
                    target=target,
                    face_enabled=True,
                    openclip_enabled=True,
                )
                content = b""
                content_type = ""
                body = ""
                status = HTTPStatus.OK

                def respond_bytes(
                    self,
                    content: bytes,
                    content_type: str,
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    self.content = content
                    self.content_type = content_type
                    self.status = status

                def respond_text(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    self.body = content
                    self.status = status

                def respond_html(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    self.body = content
                    self.status = status

            with patch("bildebank.server_app.server_program_repo_root", return_value=root):
                handler = FakeHandler()
                BildebankRequestHandler.respond_help(handler, "screenshots/bildebank.png")  # type: ignore[arg-type]
                missing_handler = FakeHandler()
                BildebankRequestHandler.respond_help(missing_handler, "screenshots/mangler.png")  # type: ignore[arg-type]
                traversal_handler = FakeHandler()
                BildebankRequestHandler.respond_help(traversal_handler, "../secret.png")  # type: ignore[arg-type]
                absolute_handler = FakeHandler()
                BildebankRequestHandler.respond_help(absolute_handler, "/screenshots/bildebank.png")  # type: ignore[arg-type]
                text_handler = FakeHandler()
                BildebankRequestHandler.respond_help(text_handler, "screenshots/not-image.txt")  # type: ignore[arg-type]

        self.assertEqual(handler.content, b"png-bytes")
        self.assertEqual(handler.content_type, "image/png")
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(missing_handler.status, HTTPStatus.NOT_FOUND)
        self.assertEqual(traversal_handler.status, HTTPStatus.FORBIDDEN)
        self.assertEqual(absolute_handler.status, HTTPStatus.FORBIDDEN)
        self.assertEqual(text_handler.status, HTTPStatus.NOT_FOUND)

    def test_run_server_routes_root_readme_to_markdown_page(self) -> None:
        handler = object.__new__(BildebankRequestHandler)
        handler.server = SimpleNamespace(read_only=False)
        handler.path = "/README.md"
        handler.routed = False
        handler.respond_readme = lambda: setattr(handler, "routed", True)

        BildebankRequestHandler.do_GET(handler)  # type: ignore[arg-type]

        self.assertTrue(handler.routed)

    def test_run_server_routes_docs_paths_to_help_handler(self) -> None:
        handler = object.__new__(BildebankRequestHandler)
        handler.server = SimpleNamespace(read_only=False)
        handler.path = "/docs/web/screenshots/bildebank.png"
        handler.routed_path = ""
        handler.respond_help = lambda raw_path: setattr(handler, "routed_path", raw_path)

        BildebankRequestHandler.do_GET(handler)  # type: ignore[arg-type]

        self.assertEqual(handler.routed_path, "web/screenshots/bildebank.png")

    def test_run_server_does_not_route_geo_missing(self) -> None:
        handler = object.__new__(BildebankRequestHandler)
        handler.server = SimpleNamespace(read_only=False, target=Path("target"))
        handler.path = "/geo/missing"
        handler.file_path = ""
        handler.respond_file = lambda raw_path: setattr(handler, "file_path", raw_path)

        BildebankRequestHandler.do_GET(handler)  # type: ignore[arg-type]

        self.assertEqual(handler.file_path, "geo/missing")

    def test_run_server_renders_root_readme_as_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "program"
            root.mkdir()
            (root / "README.md").write_text("# Bildebank\n\nSe `kommando`.", encoding="utf-8")

            class FakeHandler:
                server = SimpleNamespace(face_enabled=True, openclip_enabled=True)
                body = ""
                status = HTTPStatus.OK

                def respond_html(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    self.body = content
                    self.status = status

                def respond_text(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
                    self.body = content
                    self.status = status

            with patch("bildebank.server_app.server_program_repo_root", return_value=root):
                handler = FakeHandler()
                BildebankRequestHandler.respond_readme(handler)  # type: ignore[arg-type]

        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertIn("<h1>Bildebank</h1>", handler.body)
        self.assertIn("<code>kommando</code>", handler.body)

    def test_run_server_image_html_uses_preview_source_and_display_link(self) -> None:
        body = item_media_html(
            Path("."),
            {
                "id": 7,
                "target_path": "2024/01/image.jpg",
                "stored_filename": "image.jpg",
                "view_rotation_degrees": 0,
            },
        )

        self.assertIn('href="/display/7"', body)
        self.assertIn('src="/preview/7"', body)
        self.assertNotIn('src="/file/7"', body)

    def test_run_server_renders_index_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            init_database(target)
            server = SimpleNamespace(
                target=target,
                config=AppConfig(openclip=OpenClipConfig(enabled=True)),
                face_enabled=True,
                openclip_enabled=True,
            )
            body = index_html(server)
            disabled_server = SimpleNamespace(
                target=target,
                config=AppConfig(openclip=OpenClipConfig(enabled=False)),
                face_enabled=True,
                openclip_enabled=False,
            )
            disabled_body = index_html(disabled_server)

        self.assertIn("Bildebrowser", body)
        self.assertIn("Bildesøk", body)
        self.assertIn("Ingen filer i bildesamlingen", body)
        self.assertNotIn("Bildesøk", disabled_body)

    def test_run_server_shell_pages_use_common_topline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            init_database(target)
            config = AppConfig(openclip=OpenClipConfig(enabled=True))
            server = SimpleNamespace(
                target=target,
                config=config,
                face_enabled=True,
                openclip_enabled=True,
                search_cache=SimpleNamespace(loaded=False),
            )
            pages = [
                sources_page_html(target),
                app_status_page_html(target, config),
                markdown_doc_page_html(Path("bildebrowser.md"), "# Hjelp\n\nTekst."),
                search_html(server, ServerSearchStats("strand", ()), DEFAULT_SEARCH_LIMIT),
            ]

            for body in pages:
                self.assertIn('<header class="browser-header">', body)
                self.assertIn('<div class="topline">', body)
                self.assertIn('href="/">Alle bilder</a>', body)
                self.assertIn('href="/dashboard">Dashboard</a>', body)
                self.assertIn('href="/settings">Innstillinger</a>', body)
                self.assertIn('href="/help/web/bildebrowser">Hjelp</a>', body)

    def test_run_server_common_topline_respects_feature_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            init_database(target)
            body = sources_page_html(target, face_enabled=False, openclip_enabled=False)
            enabled_body = sources_page_html(target, openclip_enabled=True)

        self.assertIn('<header class="browser-header">', body)
        self.assertIn('href="/">Alle bilder</a>', body)
        self.assertIn('href="/geo">Steder</a>', body)
        self.assertIn('href="/dashboard">Dashboard</a>', body)
        self.assertIn('href="/search" data-search-preload>Bildesøk</a>', enabled_body)
        self.assertIn('fetch("/api/search-preload", {keepalive: true})', SERVER_JS)
        self.assertIn('link.addEventListener("pointerdown", preloadSearchModel)', SERVER_JS)
        self.assertNotIn('href="/people">Personer</a>', body)
        self.assertNotIn('href="/search">Bildesøk</a>', body)

    def test_run_server_face_enabled_uses_server_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "program"
            target = Path(tmp) / "target"
            root.mkdir()
            target.mkdir()
            server = object.__new__(BildebankServer)
            server.target = target
            server.config = AppConfig(face_recognition=FaceRecognitionConfig(enabled=False))

            self.assertFalse(server.face_enabled)
            (root / "bildebank-config.toml").write_text(
                "[face_recognition]\nenabled = true\n",
                encoding="utf-8",
            )
            self.assertFalse(server.face_enabled)

    def test_server_file_path_by_id_stays_inside_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            init_database(target)
            relative_path = Path("2024/01/image.jpg")
            image_path = target / relative_path
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"image")
            file_id = register_target_file(target, relative_path)

            self.assertEqual(server_file_path_by_id(target, file_id), image_path.resolve())

    def test_server_file_path_by_id_rejects_missing_file_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)

            with self.assertRaisesRegex(FileNotFoundError, "Filen finnes ikke"):
                server_file_path_by_id(target, 999)

    def test_server_file_path_by_id_rejects_database_path_outside_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            init_database(target)
            relative_path = Path("2024/01/image.jpg")
            image_path = target / relative_path
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"image")
            file_id = register_target_file(target, relative_path)
            db.prepare_database(target)
            outside_path = root / "outside.jpg"
            outside_path.write_bytes(b"outside")
            conn = db.connect(target)
            try:
                conn.execute(
                    "UPDATE files SET target_path = ? WHERE id = ?",
                    (str(outside_path), file_id),
                )
                conn.commit()
            finally:
                conn.close()

            with self.assertRaisesRegex(PermissionError, "Ugyldig filsti i databasen"):
                server_file_path_by_id(target, file_id)

    def test_run_server_rejects_post_without_csrf_token(self) -> None:
        class FakeHandler:
            path = "/api/item-tag"
            headers: dict[str, str] = {}
            rfile = BytesIO()
            server = SimpleNamespace(csrf_token="test-token")
            body: dict[str, object] | None = None
            status = None

            def respond_json(self, content: dict[str, object], *, status=None) -> None:
                self.body = content
                self.status = status

        handler = FakeHandler()
        BildebankRequestHandler.do_POST(handler)  # type: ignore[arg-type]

        self.assertEqual(HTTPStatus.FORBIDDEN, handler.status)
        self.assertEqual(
            {"ok": False, "error": "Ugyldig eller manglende CSRF-token."},
            handler.body,
        )

    def test_run_server_generates_one_csrf_token_at_startup(self) -> None:
        with (
            patch("bildebank.server_runtime.ThreadingHTTPServer.__init__", return_value=None),
            patch("bildebank.server_runtime.secrets.token_urlsafe", return_value="generated-token") as token_urlsafe,
        ):
            server = BildebankServer(("127.0.0.1", 0), Path("."), AppConfig())

        self.assertEqual(server.csrf_token, "generated-token")
        token_urlsafe.assert_called_once_with(32)

    def test_run_server_accepts_csrf_header_and_form_field(self) -> None:
        def validate(headers: dict[str, str], body: bytes = b"") -> tuple[bool, BytesIO]:
            class FakeHandler:
                rfile = BytesIO(body)
                server = SimpleNamespace(csrf_token="test-token")
                response = None

                def respond_json(self, content: dict[str, object], *, status=None) -> None:
                    self.response = (content, status)

            handler = FakeHandler()
            handler.headers = headers
            accepted = BildebankRequestHandler.validate_csrf_request(handler)  # type: ignore[arg-type]
            return accepted, handler.rfile

        header_accepted, _ = validate({"X-CSRF-Token": "test-token"})
        form = b"csrf_token=test-token&name=Familie"
        form_accepted, restored_body = validate(
            {
                "Content-Length": str(len(form)),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            form,
        )

        self.assertTrue(header_accepted)
        self.assertTrue(form_accepted)
        self.assertEqual(restored_body.read(), form)

    def test_run_server_html_includes_csrf_meta_and_post_form_fields(self) -> None:
        content = (
            "<!doctype html><html><head><title>Test</title></head><body>"
            '<form method="post" action="/save"></form>'
            '<form action="/other" method="POST"><button>OK</button></form>'
            '<form method="get" action="/search"></form>'
            "</body></html>"
        )

        rendered = add_csrf_to_html(content, 'token<&"')

        self.assertIn(
            '<meta name="csrf-token" content="token&lt;&amp;&quot;">',
            rendered,
        )
        self.assertEqual(rendered.count('name="csrf_token"'), 2)
        self.assertIn(
            '<input type="hidden" name="csrf_token" value="token&lt;&amp;&quot;">',
            rendered,
        )
        self.assertIn('const csrfToken = document.querySelector', SERVER_JS)
        self.assertIn('headers.set("X-CSRF-Token", csrfToken);', SERVER_JS)
        self.assertNotIn('await fetch("/api/', SERVER_JS)

    def test_run_server_confirm_messages_use_javascript_newlines(self) -> None:
        self.assertIn("Tilsvarer:\\n${command}", SERVER_JS)
        self.assertNotIn("Tilsvarer:\\\\n${command}", SERVER_JS)
        self.assertIn("Flytte til deleted/?\\n\\n${path}", SERVER_JS)
        self.assertNotIn("Flytte til deleted/?\\\\n\\\\n${path}", SERVER_JS)
        self.assertIn("window.location.href = payload.redirect_url", SERVER_JS)
