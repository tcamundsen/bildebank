from __future__ import annotations

import http.client
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http import HTTPStatus
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bildebank import db, server_request
from bildebank.cli import (
    build_parser,
    main,
    run_target_command,
    should_recover_pending_file_moves,
    validate_parsed_args,
)
from bildebank.cli_server import lan_share_urls, run_server_command
from bildebank.config import AppConfig, BrowserConfig, FaceRecognitionConfig, OpenClipConfig
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
    validate_remote_write,
)
from bildebank.server_assets import SERVER_JS
from bildebank.server_browser_item_html import item_media_html
from bildebank.target_lock import LOCK_FILENAME
from bildebank.server_files import (
    open_server_file,
    resolve_server_file,
    server_file_path,
    server_file_path_by_id,
)
from bildebank.server_pages import (
    app_status_page_html,
    index_html,
    markdown_doc_page_html,
    search_html,
    sources_page_html,
)
from bildebank.server_response import (
    SECURITY_RESPONSE_HEADERS,
    ServerResponseMixin,
    add_csrf_to_html,
    error_message_for_client,
    read_only_html,
    send_security_response_headers,
)
from bildebank.server_search import DEFAULT_SEARCH_LIMIT, ServerSearchStats
from tests.cli_helpers import write_test_image
from tests.db_test_helpers import insert_test_file, register_target_file


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
        self.assertIn("--allow-remote-write", stdout)
        self.assertEqual(stderr_buffer.getvalue(), "")

    def test_read_only_html_removes_only_generated_settings_navigation_links(self) -> None:
        body = """
        <a class="server-search-link" href="/settings">Innstillinger</a>
        <a href="/settings">Innstillinger</a>
        <p>Se innstillinger for mer informasjon.</p>
        <a href="/settings/removed">Slettede bilder</a>
        """

        result = read_only_html(body)

        self.assertNotIn('href="/settings">Innstillinger</a>', result)
        self.assertIn("Se innstillinger for mer informasjon.", result)
        self.assertIn('href="/settings/removed">Slettede bilder</a>', result)

    def test_redirect_sends_safe_location(self) -> None:
        class Handler(ServerResponseMixin):
            def __init__(self) -> None:
                self.events: list[tuple[str, object]] = []

            def send_response(self, status: int, message: str | None = None) -> None:
                self.events.append(("status", status))

            def send_header(self, name: str, value: str) -> None:
                self.events.append((name, value))

            def end_headers(self) -> None:
                self.events.append(("end_headers", None))

        handler = Handler()
        handler.redirect("/item/1?from=people#face-2")

        self.assertEqual(
            handler.events,
            [
                ("status", HTTPStatus.FOUND),
                ("Location", "/item/1?from=people#face-2"),
                ("Content-Length", "0"),
                ("end_headers", None),
            ],
        )

    def test_redirect_rejects_response_header_line_breaks(self) -> None:
        class Handler(ServerResponseMixin):
            def __init__(self) -> None:
                self.events: list[tuple[str, object]] = []

            def send_response(self, status: int, message: str | None = None) -> None:
                self.events.append(("status", status))

            def send_header(self, name: str, value: str) -> None:
                self.events.append((name, value))

            def end_headers(self) -> None:
                self.events.append(("end_headers", None))

        for line_break in ("\r", "\n", "\r\n"):
            with self.subTest(line_break=repr(line_break)):
                handler = Handler()

                with self.assertRaisesRegex(ValueError, "linjeskift"):
                    handler.redirect(f"/item/1{line_break}X-Injected: true")

                self.assertEqual(handler.events, [])

    def test_security_response_headers_are_fixed_and_central(self) -> None:
        class Handler:
            def __init__(self) -> None:
                self.headers: list[tuple[str, str]] = []

            def send_header(self, name: str, value: str) -> None:
                self.headers.append((name, value))

        handler = Handler()
        send_security_response_headers(handler)

        self.assertEqual(handler.headers, list(SECURITY_RESPONSE_HEADERS))
        self.assertIn(("Referrer-Policy", "same-origin"), handler.headers)

    def test_shared_profiles_hide_exception_details_from_clients(self) -> None:
        details = OSError(r"unable to open C:\Users\Tom\Bilder\.bilder.sqlite3")
        local_server = SimpleNamespace(lan_share=False, slideshow=None)
        lan_share_server = SimpleNamespace(lan_share=True, slideshow=None)
        slideshow_server = SimpleNamespace(lan_share=False, slideshow=object())

        self.assertEqual(
            error_message_for_client(
                local_server,
                details,
                shared_message="Kunne ikke vise siden.",
            ),
            str(details),
        )
        for profile, server in (
            ("lan-share", lan_share_server),
            ("slideshow", slideshow_server),
        ):
            with self.subTest(profile=profile):
                self.assertEqual(
                    error_message_for_client(
                        server,
                        details,
                        shared_message="Kunne ikke vise siden.",
                    ),
                    "Kunne ikke vise siden.",
                )

    def test_lan_share_hides_unexpected_get_exception_details(self) -> None:
        private_path = r"C:\Users\Tom\Bilder\.bilder.sqlite3"
        handler = object.__new__(BildebankRequestHandler)
        handler.path = "/"
        handler.server = SimpleNamespace(
            face_enabled=False,
            lan_share=True,
            openclip_enabled=False,
            read_only=True,
            slideshow=None,
        )
        response: dict[str, object] = {}
        handler.respond_html = lambda content, *, status=HTTPStatus.OK: response.update(
            content=content,
            status=status,
        )

        with patch(
            "bildebank.server_endpoints_browser.respond_browser_root",
            side_effect=OSError(f"unable to open {private_path}"),
        ):
            BildebankRequestHandler.do_GET(handler)

        self.assertEqual(response["status"], HTTPStatus.INTERNAL_SERVER_ERROR)
        self.assertIn("Kunne ikke vise siden.", str(response["content"]))
        self.assertNotIn(private_path, str(response["content"]))

    def test_lan_share_hides_file_exception_details(self) -> None:
        private_path = r"C:\Users\Tom\Bilder\2024\01\privat.jpg"
        handler = object.__new__(BildebankRequestHandler)
        handler.server = SimpleNamespace(
            lan_share=True,
            read_only=True,
            slideshow=None,
            target=Path("target"),
        )
        response: dict[str, object] = {}
        handler.respond_text = lambda content, *, status=HTTPStatus.OK: response.update(
            content=content,
            status=status,
        )

        with patch(
            "bildebank.server_files.resolve_server_file",
            side_effect=FileNotFoundError(f"Filen finnes ikke: {private_path}"),
        ):
            BildebankRequestHandler.respond_file(handler, "1")

        self.assertEqual(response["status"], HTTPStatus.NOT_FOUND)
        self.assertEqual(response["content"], "Filen finnes ikke.")
        self.assertNotIn(private_path, str(response["content"]))

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

    def test_read_only_server_does_not_record_target_in_program_database(self) -> None:
        for option in ("--read-only", "--lan-share", "--slideshow"):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "target"
                init_database(target)

                with patch("bildebank.cli.run_server_command", return_value=0):
                    code = main(
                        [
                            "--target",
                            str(target),
                            "run-server",
                            option,
                            "--no-browser",
                        ]
                    )

                self.assertEqual(code, 0)
        self.assertFalse((self.program_root / ".bildebank-program.sqlite3").exists())

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

    def test_browser_request_connection_is_always_read_only(self) -> None:
        handler = object.__new__(BildebankRequestHandler)
        handler.server = SimpleNamespace(target=Path("target"))
        fake_connection = object()

        with (
            patch(
                "bildebank.server_handler.db.connect",
                side_effect=AssertionError("browser-GET skal ikke åpne skrivbart"),
            ),
            patch(
                "bildebank.server_handler.db.connect_read_only",
                return_value=fake_connection,
            ) as connect_read_only,
        ):
            connection, close_connection = handler.browser_db_connection()

        self.assertIs(connection, fake_connection)
        self.assertTrue(close_connection)
        connect_read_only.assert_called_once_with(Path("target"))

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

        parser = build_parser()
        args = parser.parse_args(
            [
                "run-server",
                "--host",
                "0.0.0.0",
                "--allow-remote",
                "--allow-remote-write",
                "--no-browser",
            ]
        )
        validate_parsed_args(parser, args)
        self.assertTrue(args.allow_remote)
        self.assertTrue(args.allow_remote_write)
        self.assertEqual(args.host, "0.0.0.0")

    def test_run_server_remote_write_requires_separate_permission(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["run-server", "--host", "0.0.0.0", "--allow-remote"]
        )
        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            validate_parsed_args(parser, args)

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--allow-remote-write", stderr.getvalue())

        read_only_args = parser.parse_args(
            [
                "run-server",
                "--host",
                "0.0.0.0",
                "--allow-remote",
                "--read-only",
            ]
        )
        validate_parsed_args(parser, read_only_args)

    def test_run_server_forwards_explicit_remote_write_permission(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "run-server",
                "--host",
                "0.0.0.0",
                "--allow-remote",
                "--allow-remote-write",
                "--no-browser",
            ]
        )
        validate_parsed_args(parser, args)

        with patch("bildebank.cli.run_server_command", return_value=0) as run_server:
            result = run_target_command(args, Path("C:/Users/Tom/Bilder"))

        self.assertEqual(result, 0)
        self.assertTrue(run_server.call_args.kwargs["allow_remote"])
        self.assertTrue(run_server.call_args.kwargs["allow_remote_write"])
        self.assertFalse(run_server.call_args.kwargs["read_only"])

    def test_run_server_remote_write_permission_rejects_incompatible_modes(self) -> None:
        cases = (
            (
                "missing remote permission",
                ["run-server", "--host", "0.0.0.0", "--allow-remote-write"],
                "krever også --allow-remote",
            ),
            (
                "read only",
                [
                    "run-server",
                    "--host",
                    "0.0.0.0",
                    "--allow-remote",
                    "--allow-remote-write",
                    "--read-only",
                ],
                "read-only",
            ),
            (
                "local host",
                [
                    "run-server",
                    "--host",
                    "127.0.0.1",
                    "--allow-remote",
                    "--allow-remote-write",
                ],
                "ekstern --host",
            ),
        )
        for label, argv, expected in cases:
            with self.subTest(label=label):
                parser = build_parser()
                args = parser.parse_args(argv)
                stderr = StringIO()
                with redirect_stderr(stderr), self.assertRaises(SystemExit):
                    validate_parsed_args(parser, args)
                self.assertIn(expected, stderr.getvalue())

    def test_runtime_rejects_remote_write_without_separate_permission(self) -> None:
        validate_remote_write(
            "127.0.0.1",
            read_only=False,
            allow_remote_write=False,
        )
        validate_remote_write(
            "0.0.0.0",
            read_only=True,
            allow_remote_write=False,
        )
        with self.assertRaisesRegex(ValueError, "--allow-remote-write"):
            validate_remote_write(
                "0.0.0.0",
                read_only=False,
                allow_remote_write=False,
            )

    def test_request_authority_accepts_local_and_lan_addresses(self) -> None:
        cases = (
            (
                {"Host": "127.0.0.1:8765"},
                "127.0.0.1",
                "127.0.0.1",
            ),
            (
                {"Host": "127.0.0.1"},
                "127.0.0.1",
                "127.0.0.1",
            ),
            (
                {
                    "Host": "localhost:8765",
                    "Origin": "http://localhost:8765",
                },
                "127.0.0.1",
                "127.0.0.1",
            ),
            (
                {
                    "Host": "192.168.86.11:8765",
                    "Origin": "http://192.168.86.11:8765",
                },
                "0.0.0.0",
                "0.0.0.0",
            ),
            (
                {
                    "Host": "my-pc:8765",
                    "Origin": "http://my-pc:8765",
                },
                "my-pc",
                "192.168.86.11",
            ),
        )

        for headers, bind_host, server_host in cases:
            with self.subTest(headers=headers, bind_host=bind_host):
                server_request.validate_request_authority(
                    headers,
                    bind_host=bind_host,
                    server_host=server_host,
                    server_port=8765,
                )

    def test_request_authority_rejects_rebinding_and_ambiguous_headers(
        self,
    ) -> None:
        class RepeatedHeaders(dict[str, str]):
            repeated: dict[str, list[str]]

            def get_all(self, name: str) -> list[str] | None:
                return self.repeated.get(name.casefold())

        cases: tuple[
            str,
            dict[str, str] | RepeatedHeaders,
            HTTPStatus,
        ] = (
            (
                "hostname through wildcard bind",
                {"Host": "attacker.example:8765"},
                HTTPStatus.MISDIRECTED_REQUEST,
            ),
            (
                "cross-origin",
                {
                    "Host": "127.0.0.1:8765",
                    "Origin": "http://attacker.example:8765",
                },
                HTTPStatus.FORBIDDEN,
            ),
            (
                "wrong origin port",
                {
                    "Host": "127.0.0.1:8765",
                    "Origin": "http://127.0.0.1:9000",
                },
                HTTPStatus.FORBIDDEN,
            ),
            (
                "wrong host port",
                {"Host": "127.0.0.1:9000"},
                HTTPStatus.MISDIRECTED_REQUEST,
            ),
            (
                "missing host",
                {},
                HTTPStatus.BAD_REQUEST,
            ),
            (
                "invalid host",
                {"Host": "127.0.0.1:invalid"},
                HTTPStatus.BAD_REQUEST,
            ),
            (
                "null origin",
                {
                    "Host": "127.0.0.1:8765",
                    "Origin": "null",
                },
                HTTPStatus.BAD_REQUEST,
            ),
        )
        repeated_host = RepeatedHeaders({"Host": "127.0.0.1:8765"})
        repeated_host.repeated = {
            "host": ["127.0.0.1:8765", "attacker.example:8765"],
        }
        repeated_origin = RepeatedHeaders(
            {
                "Host": "127.0.0.1:8765",
                "Origin": "http://127.0.0.1:8765",
            }
        )
        repeated_origin.repeated = {
            "host": ["127.0.0.1:8765"],
            "origin": [
                "http://127.0.0.1:8765",
                "http://attacker.example:8765",
            ],
        }
        cases += (
            ("repeated host", repeated_host, HTTPStatus.BAD_REQUEST),
            ("repeated origin", repeated_origin, HTTPStatus.BAD_REQUEST),
        )

        for label, headers, expected_status in cases:
            with self.subTest(label=label):
                with self.assertRaises(server_request.RequestAuthorityError) as raised:
                    server_request.validate_request_authority(
                        headers,
                        bind_host="0.0.0.0",
                        server_host="0.0.0.0",
                        server_port=8765,
                    )

                self.assertEqual(raised.exception.status, expected_status)

    def test_http_server_rejects_rebinding_host_and_cross_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            relative_path = Path("2024/01/image.png")
            write_test_image(target / relative_path)
            file_id = register_target_file(target, relative_path)
            expected_file_content = (target / relative_path).read_bytes()
            try:
                server = BildebankServer(
                    ("127.0.0.1", 0),
                    target,
                    AppConfig(browser=BrowserConfig(hide_out_of_focus=True)),
                )
            except PermissionError as exc:
                self.skipTest(f"Miljøet tillater ikke lokal HTTP-socket: {exc}")
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            port = server.server_address[1]

            def request(
                path: str,
                headers: dict[str, str],
                *,
                method: str = "GET",
                body: bytes = b"",
            ) -> tuple[int, dict[str, str], bytes]:
                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    port,
                    timeout=5,
                )
                try:
                    connection.putrequest(method, path, skip_host=True)
                    for name, value in headers.items():
                        connection.putheader(name, value)
                    if body:
                        connection.putheader("Content-Length", str(len(body)))
                    connection.putheader("Connection", "close")
                    connection.endheaders()
                    if body:
                        connection.send(body)
                    response = connection.getresponse()
                    return (
                        response.status,
                        dict(response.getheaders()),
                        response.read(),
                    )
                finally:
                    connection.close()

            try:
                valid_status, valid_headers, valid_body = request(
                    "/",
                    {
                        "Host": f"127.0.0.1:{port}",
                        "Origin": f"http://127.0.0.1:{port}",
                    }
                )
                file_status, file_headers, file_body = request(
                    f"/file/{file_id}",
                    {"Host": f"127.0.0.1:{port}"},
                )
                redirect_status, redirect_headers, redirect_body = request(
                    "/filter?q=year%3A2024",
                    {"Host": f"127.0.0.1:{port}"},
                )
                rebinding_status, rebinding_headers, rebinding_body = request(
                    "/",
                    {"Host": f"attacker.example:{port}"}
                )
                cross_origin_status, cross_origin_headers, cross_origin_body = request(
                    "/",
                    {
                        "Host": f"127.0.0.1:{port}",
                        "Origin": f"http://attacker.example:{port}",
                    }
                )
                settings_body = (
                    f"csrf_token={server.csrf_token}&enabled=false"
                ).encode("ascii")
                with patch(
                    "bildebank.server_app.server_program_repo_root",
                    return_value=self.program_root,
                ):
                    settings_status, settings_headers, settings_body_response = request(
                        "/settings/hide-out-of-focus",
                        {
                            "Host": f"127.0.0.1:{port}",
                            "Origin": f"http://127.0.0.1:{port}",
                            "Content-Type": "application/x-www-form-urlencoded",
                        },
                        method="POST",
                        body=settings_body,
                    )
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

        self.assertEqual(valid_status, HTTPStatus.OK)
        self.assertIn(b"<!doctype html>", valid_body)
        self.assertEqual(file_status, HTTPStatus.OK)
        self.assertEqual(file_body, expected_file_content)
        self.assertEqual(redirect_status, HTTPStatus.FOUND)
        self.assertEqual(redirect_body, b"")
        self.assertEqual(rebinding_status, HTTPStatus.MISDIRECTED_REQUEST)
        self.assertIn(b"Host-headeren er ikke tillatt", rebinding_body)
        self.assertEqual(cross_origin_status, HTTPStatus.FORBIDDEN)
        self.assertIn(b"Origin-headeren stemmer ikke", cross_origin_body)
        self.assertEqual(settings_status, HTTPStatus.FOUND)
        self.assertEqual(settings_body_response, b"")
        self.assertFalse(server.config.browser.hide_out_of_focus)
        for response_headers in (
            valid_headers,
            file_headers,
            redirect_headers,
            rebinding_headers,
            cross_origin_headers,
            settings_headers,
        ):
            with self.subTest(response_headers=response_headers):
                self.assertEqual(response_headers["Server"], "Bildebank")
                for name, value in SECURITY_RESPONSE_HEADERS:
                    self.assertEqual(response_headers[name], value)

    def test_run_server_command_forwards_remote_permission(self) -> None:
        config = AppConfig()
        with (
            patch("bildebank.cli_server.load_config", return_value=config) as load_config,
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
        load_config.assert_called_once_with(self.program_root, migrate_legacy=False)
        run_local_server.assert_called_once()
        self.assertEqual(run_local_server.call_args.kwargs["host"], "0.0.0.0")
        self.assertTrue(run_local_server.call_args.kwargs["allow_remote"])
        self.assertFalse(run_local_server.call_args.kwargs["allow_remote_write"])
        self.assertTrue(run_local_server.call_args.kwargs["preview_images"])
        self.assertTrue(run_local_server.call_args.kwargs["read_only"])
        self.assertTrue(run_local_server.call_args.kwargs["lan_share"])
        output = stdout.getvalue()
        self.assertIn("LAN-share er aktiv", output)
        self.assertIn("Lokale kilde- og snapshotbaner skjules", output)
        self.assertIn("nøyaktig GPS", output)
        self.assertIn("Serveren kan nås av alle på samme LAN", output)
        self.assertIn("Ikke bruk --lan-share på offentlige nettverk", output)
        self.assertIn("http://192.168.86.11:8765/", output)

    def test_run_server_command_rejects_remote_write_before_loading_config(self) -> None:
        with (
            patch("bildebank.cli_server.load_config") as load_config,
            patch("bildebank.cli_server.run_local_server") as run_local_server,
            self.assertRaisesRegex(ValueError, "--allow-remote-write"),
        ):
            run_server_command(
                Path("C:/Users/Tom/Bilder"),
                host="0.0.0.0",
                port=8765,
                repo_root=self.program_root,
                browser=False,
                allow_remote=True,
            )

        load_config.assert_not_called()
        run_local_server.assert_not_called()

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

    def test_read_only_server_does_not_migrate_legacy_config(self) -> None:
        config_path = self.program_root / "bildebank-config.toml"
        config_path.write_text(
            "[openclip]\n"
            "enabled = false\n"
            'model_name = "ViT-B-32"\n',
            encoding="utf-8",
        )
        original = config_path.read_bytes()
        with (
            patch("bildebank.cli_server.run_local_server"),
            redirect_stdout(StringIO()),
        ):
            result = run_server_command(
                Path("C:/Users/Tom/Bilder"),
                host="127.0.0.1",
                port=8765,
                repo_root=self.program_root,
                browser=False,
                read_only=True,
            )

        self.assertEqual(result, 0)
        self.assertEqual(config_path.read_bytes(), original)

    def test_run_server_warns_before_allowed_remote_write(self) -> None:
        fake_server = SimpleNamespace(
            server_address=("0.0.0.0", 8765),
            serve_forever=lambda: None,
            server_close=lambda: None,
        )
        stderr = StringIO()
        with (
            patch(
                "bildebank.server_runtime.db.prepare_database"
            ) as prepare_database,
            patch("bildebank.server_runtime.BildebankServer", return_value=fake_server) as server_class,
            redirect_stderr(stderr),
        ):
            run_http_server(
                Path("."),
                AppConfig(),
                host="0.0.0.0",
                allow_remote=True,
                allow_remote_write=True,
            )

        prepare_database.assert_called_once_with(Path("."))
        server_class.assert_called_once_with(
            ("0.0.0.0", 8765),
            Path("."),
            AppConfig(),
            preview_images=False,
            read_only=False,
            lan_share=False,
            slideshow=None,
        )
        self.assertIn("ADVARSEL", stderr.getvalue())
        self.assertIn("skrivbar", stderr.getvalue())
        self.assertIn("deleted/", stderr.getvalue())

    def test_run_server_rejects_remote_write_before_database_preparation(self) -> None:
        with (
            patch("bildebank.server_runtime.db.prepare_database") as prepare_database,
            patch("bildebank.server_runtime.BildebankServer") as server_class,
            self.assertRaisesRegex(ValueError, "--allow-remote-write"),
        ):
            run_http_server(
                Path("."),
                AppConfig(),
                host="0.0.0.0",
                allow_remote=True,
            )

        prepare_database.assert_not_called()
        server_class.assert_not_called()

    def test_run_server_creates_server_in_read_only_mode(self) -> None:
        fake_server = SimpleNamespace(
            server_address=("127.0.0.1", 8765),
            serve_forever=lambda: None,
            server_close=lambda: None,
        )
        with (
            patch("bildebank.server_runtime.db.prepare_database") as prepare_database,
            patch(
                "bildebank.server_runtime.db.prepare_database_read_only"
            ) as prepare_database_read_only,
            patch("bildebank.server_runtime.BildebankServer", return_value=fake_server) as server_class,
        ):
            run_http_server(Path("."), AppConfig(), read_only=True)

        prepare_database.assert_not_called()
        prepare_database_read_only.assert_called_once_with(Path("."))
        server_class.assert_called_once_with(
            ("127.0.0.1", 8765),
            Path("."),
            AppConfig(),
            preview_images=False,
            read_only=True,
            lan_share=False,
            slideshow=None,
        )

    def test_run_server_forwards_lan_share_profile(self) -> None:
        fake_server = SimpleNamespace(
            server_address=("0.0.0.0", 8765),
            serve_forever=lambda: None,
            server_close=lambda: None,
        )
        with (
            patch(
                "bildebank.server_runtime.db.prepare_database_read_only"
            ) as prepare_database_read_only,
            patch(
                "bildebank.server_runtime.BildebankServer",
                return_value=fake_server,
            ) as server_class,
            redirect_stderr(StringIO()),
        ):
            run_http_server(
                Path("."),
                AppConfig(),
                host="0.0.0.0",
                allow_remote=True,
                preview_images=True,
                read_only=True,
                lan_share=True,
            )

        prepare_database_read_only.assert_called_once_with(Path("."))
        server_class.assert_called_once_with(
            ("0.0.0.0", 8765),
            Path("."),
            AppConfig(),
            preview_images=True,
            read_only=True,
            lan_share=True,
            slideshow=None,
        )

    def test_read_only_server_start_leaves_collection_files_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            before = {
                path.relative_to(target): path.read_bytes()
                for path in target.rglob("*")
                if path.is_file()
            }
            fake_server = SimpleNamespace(
                server_address=("127.0.0.1", 8765),
                serve_forever=lambda: None,
                server_close=lambda: None,
            )

            with patch(
                "bildebank.server_runtime.BildebankServer",
                return_value=fake_server,
            ):
                run_http_server(target, AppConfig(), read_only=True)

            after = {
                path.relative_to(target): path.read_bytes()
                for path in target.rglob("*")
                if path.is_file()
            }

        self.assertEqual(after, before)

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
            conn = db.connect(target)
            try:
                conn.execute(
                    "UPDATE files SET view_rotation_degrees = 90 WHERE id = ?",
                    (file_id,),
                )
                conn.commit()
            finally:
                conn.close()

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
        self.assertIn(
            'csrfFetch("/api/search-preload", {method: "POST", keepalive: true})',
            SERVER_JS,
        )
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

    def test_server_file_path_resolves_file_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            image_path = target / "2024" / "01" / "Januar bilde.jpg"
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"image")
            file_id = register_target_file(
                target, Path("2024/01/Januar bilde.jpg")
            )

            self.assertEqual(
                server_file_path(target, str(file_id)),
                image_path.resolve(),
            )

    def test_server_file_path_rejects_text_path_inside_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            image_path = target / "2024" / "image.jpg"
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"image")

            with self.assertRaisesRegex(FileNotFoundError, "Filen finnes ikke"):
                server_file_path(target, "2024/image.jpg")

    def test_resolve_server_file_rejects_collection_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)

            with self.assertRaisesRegex(FileNotFoundError, "Filen finnes ikke"):
                resolve_server_file(target, ".bilder.sqlite3")

    def test_server_file_path_by_id_rejects_missing_file_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)

            with self.assertRaisesRegex(FileNotFoundError, "Filen finnes ikke"):
                server_file_path_by_id(target, 999)

    def test_server_file_path_by_id_can_require_active_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            file_id = insert_test_file(
                target,
                "deleted/2024/01/image.png",
                deleted=True,
            )

            self.assertEqual(
                server_file_path_by_id(target, file_id),
                (target / "deleted/2024/01/image.png").resolve(),
            )
            with self.assertRaisesRegex(FileNotFoundError, "Filen finnes ikke"):
                server_file_path_by_id(target, file_id, require_active=True)

    def test_read_only_server_media_endpoints_reject_deleted_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            file_id = insert_test_file(
                target,
                "deleted/2024/01/image.png",
                deleted=True,
            )

            class FakeHandler:
                server = SimpleNamespace(
                    target=target,
                    preview_images=True,
                    read_only=True,
                )
                status = HTTPStatus.OK

                def reset(self) -> None:
                    self.status = HTTPStatus.OK

                def respond_bytes(
                    self,
                    content: bytes,
                    content_type: str,
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    self.status = status

                def respond_text(
                    self,
                    content: str,
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    self.status = status

                def respond_preview_image(
                    self,
                    requested_file_id: int,
                    **kwargs: object,
                ) -> bool:
                    return BildebankRequestHandler.respond_preview_image(  # type: ignore[arg-type]
                        self,
                        requested_file_id,
                        **kwargs,
                    )

                def respond_file(self, encoded_relative_path: str) -> None:
                    BildebankRequestHandler.respond_file(  # type: ignore[arg-type]
                        self,
                        encoded_relative_path,
                    )

                def respond_server_file(self, served_file: object) -> None:
                    self.status = HTTPStatus.OK

            handler = FakeHandler()
            endpoints = (
                BildebankRequestHandler.respond_display,
                BildebankRequestHandler.respond_preview,
                BildebankRequestHandler.respond_file,
                BildebankRequestHandler.respond_thumbnail,
            )
            for endpoint in endpoints:
                with self.subTest(endpoint=endpoint.__name__):
                    handler.reset()
                    endpoint(handler, str(file_id))  # type: ignore[arg-type]
                    self.assertEqual(handler.status, HTTPStatus.NOT_FOUND)

    def test_writable_server_file_endpoint_keeps_deleted_file_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            file_id = insert_test_file(
                target,
                "deleted/2024/01/image.png",
                deleted=True,
            )
            expected = (target / "deleted/2024/01/image.png").read_bytes()

            class FakeHandler:
                server = SimpleNamespace(target=target, read_only=False)
                content = b""
                status = HTTPStatus.OK

                def respond_bytes(
                    self,
                    content: bytes,
                    content_type: str,
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    self.content = content
                    self.status = status

                def respond_text(
                    self,
                    content: str,
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    self.status = status

            handler = FakeHandler()
            BildebankRequestHandler.respond_file(  # type: ignore[arg-type]
                handler,
                str(file_id),
            )

            self.assertEqual(handler.status, HTTPStatus.OK)
            self.assertEqual(handler.content, expected)

    def test_resolve_server_file_rejects_internal_file_from_corrupt_database_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            relative_path = Path("2024/01/image.jpg")
            image_path = target / relative_path
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"image")
            file_id = register_target_file(target, relative_path)
            conn = db.connect(target)
            try:
                conn.execute(
                    """
                    UPDATE files
                    SET target_path = '.bilder.sqlite3',
                        target_path_key = '.bilder.sqlite3'
                    WHERE id = ?
                    """,
                    (file_id,),
                )
                conn.commit()
            finally:
                conn.close()

            with self.assertRaisesRegex(
                PermissionError,
                "Ugyldig filsti i databasen",
            ):
                resolve_server_file(target, str(file_id))

    def test_resolve_server_file_rejects_symlink_at_database_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            relative_path = Path("2024/01/image.jpg")
            image_path = target / relative_path
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"image")
            file_id = register_target_file(target, relative_path)
            image_path.unlink()
            try:
                image_path.symlink_to(target / db.DB_FILENAME)
            except OSError as exc:
                self.skipTest(f"Miljøet tillater ikke filsymlink: {exc}")

            with self.assertRaisesRegex(
                PermissionError,
                "vanlig fil uten lenker",
            ):
                resolve_server_file(target, str(file_id))

    def test_resolve_server_file_rejects_symlinked_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            relative_path = Path("2024/01/image.jpg")
            image_path = target / relative_path
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"image")
            file_id = register_target_file(target, relative_path)
            real_year_directory = target / "stored-2024"
            (target / "2024").replace(real_year_directory)
            try:
                (target / "2024").symlink_to(
                    real_year_directory,
                    target_is_directory=True,
                )
            except OSError as exc:
                real_year_directory.replace(target / "2024")
                self.skipTest(f"Miljøet tillater ikke katalogsymlink: {exc}")

            with self.assertRaisesRegex(
                PermissionError,
                "symlink eller et Windows reparse point",
            ):
                resolve_server_file(target, str(file_id))

    def test_open_server_file_rejects_file_replaced_after_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            init_database(target)
            relative_path = Path("2024/01/image.jpg")
            image_path = target / relative_path
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"image")
            file_id = register_target_file(target, relative_path)
            served_file = resolve_server_file(target, str(file_id))

            replacement = image_path.with_name("replacement.jpg")
            replacement.write_bytes(b"other")
            replacement.replace(image_path)

            with self.assertRaisesRegex(
                PermissionError,
                "byttet eller endret",
            ):
                open_server_file(served_file)

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

    def test_run_server_rejects_unsafe_request_body_before_endpoint(self) -> None:
        cases = (
            (
                "too large",
                {
                    "Content-Length": str(
                        server_request.MAX_REQUEST_BODY_BYTES + 1
                    ),
                    "X-CSRF-Token": "test-token",
                },
                b"",
                HTTPStatus.CONTENT_TOO_LARGE,
            ),
            (
                "invalid length",
                {
                    "Content-Length": "not-a-number",
                    "X-CSRF-Token": "test-token",
                },
                b"",
                HTTPStatus.BAD_REQUEST,
            ),
            (
                "unreasonably long length",
                {
                    "Content-Length": "9" * 5000,
                    "X-CSRF-Token": "test-token",
                },
                b"",
                HTTPStatus.CONTENT_TOO_LARGE,
            ),
            (
                "negative length",
                {
                    "Content-Length": "-1",
                    "X-CSRF-Token": "test-token",
                },
                b"",
                HTTPStatus.BAD_REQUEST,
            ),
            (
                "transfer encoding",
                {
                    "Content-Length": "0",
                    "Transfer-Encoding": "chunked",
                    "X-CSRF-Token": "test-token",
                },
                b"",
                HTTPStatus.BAD_REQUEST,
            ),
            (
                "incomplete body",
                {
                    "Content-Length": "10",
                    "X-CSRF-Token": "test-token",
                },
                b"{}",
                HTTPStatus.BAD_REQUEST,
            ),
            (
                "invalid utf-8",
                {
                    "Content-Length": "1",
                    "X-CSRF-Token": "test-token",
                },
                b"\xff",
                HTTPStatus.BAD_REQUEST,
            ),
        )

        for label, headers, body, expected_status in cases:
            with self.subTest(label=label):
                class FakeHandler:
                    path = "/api/item-tag"
                    rfile = BytesIO(body)
                    server = SimpleNamespace(
                        csrf_token="test-token",
                        read_only=False,
                        slideshow=None,
                    )
                    response: tuple[dict[str, object], HTTPStatus] | None = None
                    close_connection = False

                    def respond_json(
                        self,
                        content: dict[str, object],
                        *,
                        status: HTTPStatus,
                    ) -> None:
                        self.response = content, status

                handler = FakeHandler()
                handler.headers = headers
                with patch(
                    "bildebank.server_handler.server_endpoints_items.respond_tag_item",
                    side_effect=AssertionError(
                        "endepunktet skal ikke behandle en utrygg body"
                    ),
                ):
                    BildebankRequestHandler.do_POST(handler)  # type: ignore[arg-type]

                self.assertIsNotNone(handler.response)
                assert handler.response is not None
                self.assertEqual(handler.response[1], expected_status)
                self.assertTrue(handler.close_connection)

    def test_run_server_rejects_duplicate_content_length(self) -> None:
        class DuplicateLengthHeaders(dict[str, str]):
            def get_all(self, name: str) -> list[str] | None:
                if name.casefold() == "content-length":
                    return ["2", "2"]
                return None

        class FakeHandler:
            path = "/api/item-tag"
            headers = DuplicateLengthHeaders(
                {
                    "Content-Length": "2",
                    "X-CSRF-Token": "test-token",
                }
            )
            rfile = BytesIO(b"{}")
            server = SimpleNamespace(
                csrf_token="test-token",
                read_only=False,
                slideshow=None,
            )
            response: tuple[dict[str, object], HTTPStatus] | None = None
            close_connection = False

            def respond_json(
                self,
                content: dict[str, object],
                *,
                status: HTTPStatus,
            ) -> None:
                self.response = content, status

        handler = FakeHandler()
        with patch(
            "bildebank.server_handler.server_endpoints_items.respond_tag_item",
            side_effect=AssertionError(
                "endepunktet skal ikke behandle tvetydig Content-Length"
            ),
        ):
            BildebankRequestHandler.do_POST(handler)  # type: ignore[arg-type]

        self.assertIsNotNone(handler.response)
        assert handler.response is not None
        self.assertEqual(handler.response[1], HTTPStatus.BAD_REQUEST)
        self.assertTrue(handler.close_connection)

    def test_request_body_limit_accepts_exact_maximum(self) -> None:
        body = b" " * server_request.MAX_REQUEST_BODY_BYTES

        result = server_request.read_request_body_bytes(
            {"Content-Length": str(len(body))},
            BytesIO(body),
        )

        self.assertEqual(len(result), server_request.MAX_REQUEST_BODY_BYTES)

    def test_oversized_request_body_is_rejected_without_reading_stream(self) -> None:
        class UnreadableBody:
            def read(self, size: int = -1) -> bytes:
                raise AssertionError("for stor request body skal ikke leses")

        with self.assertRaises(server_request.RequestBodyTooLarge):
            server_request.read_request_body_bytes(
                {
                    "Content-Length": str(
                        server_request.MAX_REQUEST_BODY_BYTES + 1
                    )
                },
                UnreadableBody(),
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
