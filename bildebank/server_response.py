from __future__ import annotations

import html
import json
import re
import time
from http import HTTPStatus
from io import BufferedIOBase
from typing import TYPE_CHECKING, Any

BENCHMARK_HEADER = "X-Bildebank-Benchmark"
POST_FORM_RE = re.compile(
    r"(<form\b(?=[^>]*\bmethod\s*=\s*([\"'])post\2)[^>]*>)",
    re.IGNORECASE,
)
SETTINGS_LINK_HTML = (
    '<a class="server-search-link" href="/settings">Innstillinger</a>',
    '<a href="/settings">Innstillinger</a>',
)


def add_csrf_to_html(content: str, token: str) -> str:
    escaped = html.escape(token, quote=True)
    meta = f'<meta name="csrf-token" content="{escaped}">'
    if "</head>" in content:
        content = content.replace("</head>", f"  {meta}\n</head>", 1)
    hidden = f'<input type="hidden" name="csrf_token" value="{escaped}">'
    return POST_FORM_RE.sub(lambda match: f"{match.group(1)}{hidden}", content)


def read_only_html(content: str) -> str:
    for settings_link in SETTINGS_LINK_HTML:
        content = content.replace(settings_link, "")
    return content


class ServerResponseMixin:
    if TYPE_CHECKING:
        wfile: BufferedIOBase

        def send_response(self, code: int, message: str | None = None) -> None: ...
        def send_header(self, keyword: str, value: str) -> None: ...
        def end_headers(self) -> None: ...

    def wants_benchmark_timing(self) -> bool:
        headers = getattr(self, "headers", None)
        return headers is not None and headers.get(BENCHMARK_HEADER) == "1"

    def respond_timing_headers(self) -> None:
        if not self.wants_benchmark_timing():
            return
        start = getattr(self, "request_started_at", None)
        if start is None:
            return
        elapsed = (time.perf_counter() - start) * 1000.0
        steps = dict(getattr(self, "server_timing_steps", {}))
        steps["total"] = elapsed
        timing = ", ".join(f"{name};dur={duration:.1f}" for name, duration in steps.items())
        self.send_header("Server-Timing", timing)
        self.send_header("X-Bildebank-Request-Ms", f"{elapsed:.1f}")

    def respond_html(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        server = getattr(self, "server", None)
        if getattr(server, "read_only", False):
            content = read_only_html(content)
        token = getattr(server, "csrf_token", "")
        if token:
            content = add_csrf_to_html(content, str(token))
        self.respond_bytes(content.encode("utf-8"), "text/html; charset=utf-8", status=status)

    def respond_text(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.respond_bytes(content.encode("utf-8"), "text/plain; charset=utf-8", status=status)

    def respond_json(self, content: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.respond_bytes(json.dumps(content).encode("utf-8"), "application/json; charset=utf-8", status=status)

    def respond_static_asset(self, content: str, content_type: str) -> None:
        encoded = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Length", str(len(encoded)))
        self.respond_timing_headers()
        self.end_headers()
        self.wfile.write(encoded)

    def respond_bytes(self, content: bytes, content_type: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.respond_timing_headers()
        self.end_headers()
        self.wfile.write(content)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.respond_timing_headers()
        self.end_headers()
