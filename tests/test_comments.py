from __future__ import annotations

import json
import tempfile
import unittest
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from bildebank import db, server_endpoints_items
from bildebank.server_actions import set_comment_on_file
from bildebank.server_browser_queries import (
    adjacent_browser_items,
    browser_item_by_id,
    browser_month_navigation,
)
from bildebank.server_handler import BildebankRequestHandler
from bildebank.server_pages import item_page_html
from bildebank.target_lock import LOCK_FILENAME
from tests.cli_helpers import run_cli


class CommentTests(unittest.TestCase):
    def create_imported_item(self, root: Path) -> Path:
        target = root / "target"
        source = root / "source"
        source.mkdir()
        (source / "IMG_20260102.jpg").write_bytes(b"image")
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
        return target

    def test_comment_domain_normalizes_validates_removes_and_survives_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = self.create_imported_item(Path(tmp))

            normalized = set_comment_on_file(target, 1, "  Blåbær\r\nlinje to  ")
            self.assertEqual(normalized, "Blåbær\nlinje to")
            conn = db.connect(target)
            try:
                self.assertIn("comment", {row["name"] for row in conn.execute("PRAGMA table_info(files)")})
            finally:
                conn.close()
            with self.assertRaisesRegex(ValueError, "kan ikke være tom"):
                set_comment_on_file(target, 1, " \r\n ")
            with self.assertRaisesRegex(ValueError, "2000"):
                set_comment_on_file(target, 1, "x" * 2001)
            with self.assertRaisesRegex(ValueError, "finnes ikke"):
                set_comment_on_file(target, 999, "tekst")

            self.assertEqual(
                run_cli(
                    [
                        "--target",
                        str(target),
                        "remove",
                        "2026/01/IMG_20260102.jpg",
                    ]
                ),
                0,
            )
            conn = db.connect(target)
            try:
                self.assertEqual(conn.execute("SELECT comment FROM files WHERE id = 1").fetchone()[0], normalized)
            finally:
                conn.close()
            self.assertEqual(
                run_cli(
                    [
                        "--target",
                        str(target),
                        "undelete",
                        "deleted/2026/01/IMG_20260102.jpg",
                    ]
                ),
                0,
            )
            self.assertIsNone(set_comment_on_file(target, 1, None))
            conn = db.connect(target)
            try:
                self.assertIsNone(conn.execute("SELECT comment FROM files WHERE id = 1").fetchone()[0])
            finally:
                conn.close()

    def test_comment_endpoint_handles_save_remove_invalid_payload_and_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = self.create_imported_item(Path(tmp))

            class FakeHandler:
                def __init__(self, payload: object) -> None:
                    data = json.dumps(payload).encode("utf-8")
                    self.headers = {
                        "Content-Length": str(len(data)),
                        "Content-Type": "application/json",
                    }
                    self.rfile = BytesIO(data)
                    self.server = SimpleNamespace(target=target)
                    self.body: dict[str, object] = {}
                    self.status = HTTPStatus.OK

                def respond_json(
                    self,
                    content: dict[str, object],
                    *,
                    status: HTTPStatus = HTTPStatus.OK,
                ) -> None:
                    self.body = content
                    self.status = status

            saved = FakeHandler({"file_id": 1, "comment": " hei\r\nverden "})
            server_endpoints_items.respond_comment_item(saved)  # type: ignore[arg-type]
            self.assertEqual(
                saved.body,
                {"ok": True, "file_id": 1, "comment": "hei\nverden"},
            )

            for payload in (
                {"file_id": 1},
                {"file_id": 1, "comment": 7},
                {"file_id": 1, "comment": ""},
                {"file_id": 1, "comment": "x" * 2001},
                {"file_id": 999, "comment": "ukjent"},
            ):
                with self.subTest(payload=payload):
                    handler = FakeHandler(payload)
                    server_endpoints_items.respond_comment_item(handler)  # type: ignore[arg-type]
                    self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)

            removed = FakeHandler({"file_id": 1, "comment": None})
            server_endpoints_items.respond_comment_item(removed)  # type: ignore[arg-type]
            self.assertEqual(removed.body, {"ok": True, "file_id": 1, "comment": None})

            (target / LOCK_FILENAME).write_text("command=remove\n", encoding="utf-8")
            locked = FakeHandler({"file_id": 1, "comment": "låst"})
            server_endpoints_items.respond_comment_item(locked)  # type: ignore[arg-type]
            self.assertEqual(locked.status, HTTPStatus.CONFLICT)

    def test_comment_route_uses_csrf_and_read_only_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = self.create_imported_item(Path(tmp))

            read_only = object.__new__(BildebankRequestHandler)
            read_only.server = SimpleNamespace(target=target, read_only=True, slideshow=None)
            read_only.path = "/api/item-comment"
            read_only.respond_json = Mock()  # type: ignore[method-assign]
            BildebankRequestHandler.do_POST(read_only)
            self.assertEqual(
                read_only.respond_json.call_args.kwargs["status"],
                HTTPStatus.FORBIDDEN,
            )

            no_csrf = object.__new__(BildebankRequestHandler)
            no_csrf.server = SimpleNamespace(
                target=target,
                read_only=False,
                slideshow=None,
                csrf_token="hemmelig",
            )
            no_csrf.path = "/api/item-comment"
            no_csrf.headers = {"Content-Length": "0"}  # type: ignore[assignment]
            no_csrf.rfile = BytesIO()
            no_csrf.respond_json = Mock()  # type: ignore[method-assign]
            BildebankRequestHandler.do_POST(no_csrf)
            self.assertEqual(
                no_csrf.respond_json.call_args.kwargs["status"],
                HTTPStatus.FORBIDDEN,
            )

    def test_item_page_escapes_comment_and_hides_editor_in_read_only_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = self.create_imported_item(Path(tmp))
            set_comment_on_file(target, 1, "første\n<script>alert(1)</script>")
            item = browser_item_by_id(target, 1)
            self.assertIsNotNone(item)
            args = (
                target,
                item,
                *adjacent_browser_items(target, item),
                browser_month_navigation(target, item),
            )

            writable = item_page_html(*args)
            read_only = item_page_html(*args, read_only=True)

        self.assertIn("data-open-item-comment", writable)
        self.assertIn('id="itemCommentDialog"', writable)
        self.assertIn("første\n&lt;script&gt;alert(1)&lt;/script&gt;", writable)
        self.assertNotIn("<script>alert(1)</script>", writable)
        self.assertNotIn("data-open-item-comment", read_only)
        self.assertNotIn('id="itemCommentDialog"', read_only)
        self.assertIn("data-item-comment-overlay", read_only)


if __name__ == "__main__":
    unittest.main()
