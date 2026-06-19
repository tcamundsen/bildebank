from __future__ import annotations

import json
import time
from http import HTTPStatus
from io import BufferedIOBase
from typing import TYPE_CHECKING, Any

BENCHMARK_HEADER = "X-Bildebank-Benchmark"

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
