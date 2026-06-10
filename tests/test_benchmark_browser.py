from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_benchmark_module():
    path = Path(__file__).resolve().parents[1] / "tools" / "benchmark_browser.py"
    spec = importlib.util.spec_from_file_location("benchmark_browser", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_next_link_parser_finds_next_navigation_link() -> None:
    benchmark = load_benchmark_module()
    parser = benchmark.NextLinkParser()

    parser.feed(
        """
        <a class="nav-button" href="/item/1" data-key-nav="previous">Forrige bilde</a>
        <a class="nav-button" href="/item/3" data-key-nav="next">Neste bilde</a>
        """
    )

    assert parser.next_href == "/item/3"


def test_benchmark_summary_counts_threshold_failures_and_percentiles() -> None:
    benchmark = load_benchmark_module()
    args = benchmark.parse_args(["--mode", "server", "--steps", "3", "--warmup", "0", "--threshold-ms", "200"])
    steps = [
        benchmark.StepResult(index=1, elapsed_ms=100.0, url="/item/1"),
        benchmark.StepResult(index=2, elapsed_ms=250.0, url="/item/2"),
        benchmark.StepResult(index=3, elapsed_ms=150.0, url="/item/3"),
    ]

    summary = benchmark.build_summary("server", args, steps)

    assert summary.steps_measured == 3
    assert summary.threshold_failures == 1
    assert summary.median_ms == 150.0
    assert summary.p90_ms == 250.0
    assert summary.p95_ms == 250.0


def test_keepalive_fetch_uses_path_and_query_only() -> None:
    benchmark = load_benchmark_module()

    class FakeHeaders:
        def get(self, name: str, default: str = "") -> str:
            return "text/html; charset=utf-8" if name == "Content-Type" else default

        def __contains__(self, name: str) -> bool:
            return name == "X-Bildebank-Request-Ms"

        def __getitem__(self, name: str) -> str:
            if name == "X-Bildebank-Request-Ms":
                return "12.5"
            raise KeyError(name)

    class FakeResponse:
        status = 200
        headers = FakeHeaders()

        def read(self) -> bytes:
            return b"<html></html>"

        def close(self) -> None:
            return

    class FakeConnection:
        requested_path: str | None = None

        def request(self, method: str, path: str) -> None:
            self.requested_path = path

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

    conn = FakeConnection()

    assert benchmark.fetch_text_keepalive(conn, "http://127.0.0.1:8765/item/123?x=1") == "<html></html>"
    assert conn.requested_path == "/item/123?x=1"
    html, _first_byte_ms, _read_ms, body_bytes, server_ms = benchmark.fetch_text_keepalive_timed(
        conn,
        "http://127.0.0.1:8765/item/124",
    )
    assert html == "<html></html>"
    assert body_bytes == len(b"<html></html>")
    assert server_ms == 12.5


def test_profile_summary_counts_threshold_failures_per_total_time() -> None:
    benchmark = load_benchmark_module()
    args = benchmark.parse_args(
        [
            "--mode",
            "profile",
            "--target",
            "/tmp/bilder",
            "--steps",
            "2",
            "--threshold-ms",
            "50",
        ]
    )
    steps = [
        benchmark.ProfileStepResult(
            index=1,
            url="/item/2",
            total_ms=40.0,
            connect_ms=5.0,
            item_ms=1.0,
            adjacent_ms=20.0,
            month_nav_ms=10.0,
            html_ms=9.0,
            html_bytes=100,
        ),
        benchmark.ProfileStepResult(
            index=2,
            url="/item/3",
            total_ms=60.0,
            connect_ms=6.0,
            item_ms=1.0,
            adjacent_ms=30.0,
            month_nav_ms=20.0,
            html_ms=9.0,
            html_bytes=100,
        ),
    ]

    summary = benchmark.build_profile_summary(args, steps)

    assert summary.threshold_failures == 1
    assert summary.total["median_ms"] == 50.0
    assert summary.connect["max_ms"] == 6.0
    assert summary.adjacent["max_ms"] == 30.0
