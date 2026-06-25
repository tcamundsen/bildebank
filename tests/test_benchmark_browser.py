from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


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


def test_item_page_parser_finds_hotkey_request_context() -> None:
    benchmark = load_benchmark_module()
    parser = benchmark.parse_item_page(
        """
        <meta name="csrf-token" content="csrf-123">
        <main data-browser-item-id="42" data-browser-source-url="/filter/person%3AViljar">
        </main>
        <button data-tag-toggle="42" data-tag-name="Familie" aria-pressed="false">Familie</button>
        <button data-tag-toggle="42" data-tag-name="Ute av fokus" aria-pressed="true">Ute av fokus</button>
        """
    )

    assert parser.csrf_token == "csrf-123"
    assert parser.file_id == 42
    assert parser.source_url == "/filter/person%3AViljar"
    assert parser.tags == [
        {"file_id": 42, "tag_name": "Familie", "tagged": True},
        {"file_id": 42, "tag_name": "Ute av fokus", "tagged": False},
    ]


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
            if name == "Content-Type":
                return "text/html; charset=utf-8"
            if name == "Server-Timing":
                return "parse;dur=1.5, total;dur=12.5"
            return default

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
        headers: dict[str, str] | None = None

        def request(self, method: str, path: str, headers: dict[str, str] | None = None) -> None:
            self.requested_path = path
            self.headers = headers

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

    conn = FakeConnection()

    assert benchmark.fetch_text_keepalive(conn, "http://127.0.0.1:8765/item/123?x=1") == "<html></html>"
    assert conn.requested_path == "/item/123?x=1"
    html, _first_byte_ms, _read_ms, body_bytes, server_ms, server_timing = benchmark.fetch_text_keepalive_timed(
        conn,
        "http://127.0.0.1:8765/item/124",
    )
    assert html == "<html></html>"
    assert conn.headers == {"X-Bildebank-Benchmark": "1"}
    assert body_bytes == len(b"<html></html>")
    assert server_ms == 12.5
    assert server_timing == {"parse": 1.5, "total": 12.5}


def test_parse_server_timing_header_ignores_invalid_durations() -> None:
    benchmark = load_benchmark_module()

    assert benchmark.parse_server_timing_header(
        'parse;dur=1.2, ignored;desc="missing duration", total;dur="12.8", bad;dur=nope'
    ) == {"parse": 1.2, "total": 12.8}


def test_print_summary_includes_server_timing_medians(capsys) -> None:
    benchmark = load_benchmark_module()
    args = benchmark.parse_args(["--mode", "server-keepalive", "--steps", "2", "--warmup", "0"])
    summary = benchmark.build_summary(
        "server-keepalive",
        args,
        [
            benchmark.StepResult(
                index=1,
                elapsed_ms=20.0,
                url="/item/1",
                server_timing_ms={"parse": 1.0, "db_connect": 2.0, "total": 10.0},
            ),
            benchmark.StepResult(
                index=2,
                elapsed_ms=30.0,
                url="/item/2",
                server_timing_ms={"parse": 3.0, "db_connect": 4.0, "total": 20.0},
            ),
        ],
    )

    benchmark.print_summary(summary)

    output = capsys.readouterr().out
    assert "  server: parse=2.0 ms, db_connect=3.0 ms, total=15.0 ms" in output


def test_server_timing_headers_are_opt_in() -> None:
    from bildebank.server_response import ServerResponseMixin

    class FakeHeaders:
        def __init__(self, benchmark: bool) -> None:
            self.benchmark = benchmark

        def get(self, name: str) -> str | None:
            if name == "X-Bildebank-Benchmark" and self.benchmark:
                return "1"
            return None

    class FakeHandler(ServerResponseMixin):
        def __init__(self, benchmark: bool) -> None:
            self.headers = FakeHeaders(benchmark)
            self.sent: list[tuple[str, str]] = []
            self.request_started_at = 1.0

        def send_header(self, name: str, value: str) -> None:
            self.sent.append((name, value))

    handler = FakeHandler(benchmark=False)
    handler.respond_timing_headers()
    assert handler.sent == []

    handler = FakeHandler(benchmark=True)
    handler.server_timing_steps = {"parse": 1.2}
    handler.respond_timing_headers()
    header_names = [name for name, _value in handler.sent]
    assert "Server-Timing" in header_names
    assert "X-Bildebank-Request-Ms" in header_names


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


def test_years_profile_summary_counts_threshold_failures_and_prints_steps(capsys) -> None:
    benchmark = load_benchmark_module()
    args = benchmark.parse_args(
        [
            "--mode",
            "years-profile",
            "--target",
            "/tmp/bilder",
            "--steps",
            "2",
            "--threshold-ms",
            "50",
        ]
    )
    steps = [
        benchmark.YearsProfileStepResult(
            index=1,
            total_ms=40.0,
            year_cards_ms=30.0,
            card_html_ms=5.0,
            controls_ms=1.0,
            shell_ms=4.0,
            html_bytes=1000,
            year_count=20,
        ),
        benchmark.YearsProfileStepResult(
            index=2,
            total_ms=60.0,
            year_cards_ms=45.0,
            card_html_ms=6.0,
            controls_ms=1.0,
            shell_ms=8.0,
            html_bytes=1200,
            year_count=22,
        ),
    ]

    summary = benchmark.build_years_profile_summary(args, steps)
    benchmark.print_years_profile_summary(summary)

    assert summary.threshold_failures == 1
    assert summary.total["median_ms"] == 50.0
    assert summary.year_cards["max_ms"] == 45.0
    assert summary.html_bytes_median == 1100.0
    output = capsys.readouterr().out
    assert "Bildebank years profile" in output
    assert "browser_year_cards" in output


def test_years_profile_step_renders_with_server_pages_shell(monkeypatch, tmp_path: Path) -> None:
    benchmark = load_benchmark_module()
    from bildebank import server_browser

    monkeypatch.setattr(
        server_browser,
        "browser_year_cards",
        lambda target, hide_out_of_focus=False: [
            {
                "year": "2024",
                "month_count": 1,
                "item_count": 2,
                "first_month": "2024-01",
                "item": {"target_path": "2024/01/IMG.jpg", "stored_filename": "IMG.jpg"},
            }
        ],
    )
    monkeypatch.setattr(server_browser, "year_card_html", lambda target, card: "<article>2024</article>")
    monkeypatch.setattr(server_browser, "years_navigation_controls_html", lambda cards: "<nav></nav>")

    step = benchmark.years_profile_step(tmp_path, 1)

    assert step.index == 1
    assert step.year_count == 1
    assert step.html_bytes > 0


def test_profile_parser_supports_imported_source_item_url(monkeypatch, tmp_path: Path) -> None:
    benchmark = load_benchmark_module()
    from bildebank import server_browser

    monkeypatch.setattr(
        server_browser,
        "imported_source_by_id",
        lambda target, source_id: SimpleNamespace(id=source_id, name="Minnekort"),
    )

    source, file_id = benchmark.profile_source_and_file_id(
        tmp_path,
        "http://127.0.0.1:8765/source/7/item/736",
    )

    assert source.source_id == 7
    assert source.root_url == "/source/7"
    assert file_id == 736


def test_profile_parser_rejects_unknown_imported_source(monkeypatch, tmp_path: Path) -> None:
    benchmark = load_benchmark_module()
    from bildebank import server_browser

    monkeypatch.setattr(server_browser, "imported_source_by_id", lambda target, source_id: None)

    try:
        benchmark.profile_source_and_file_id(
            tmp_path,
            "http://127.0.0.1:8765/source/999/item/736",
        )
    except RuntimeError as exc:
        assert str(exc) == "Fant ikke kilde #999."
    else:
        raise AssertionError("Ukjent source-ID skulle gi RuntimeError")


def test_parse_args_has_suite_defaults() -> None:
    benchmark = load_benchmark_module()

    args = benchmark.parse_args(["--suite", "suite.json"])

    assert args.suite == Path("suite.json")
    assert args.repeat == 3
    assert args.min_failures == 0
    assert args.max_failures == 5


def test_suite_selects_best_run_and_uses_inclusive_failure_range(tmp_path: Path, monkeypatch) -> None:
    benchmark = load_benchmark_module()
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(
        json.dumps(
            [
                {
                    "name": "vanlig-bildevisning",
                    "url": "http://127.0.0.1:8765/item/123",
                    "threshold_ms": 10,
                }
            ]
        ),
        encoding="utf-8",
    )
    results = iter(
        [
            (2, 9.0, 12.0),
            (1, 9.0, 11.0),
            (1, 8.0, 11.0),
        ]
    )

    def fake_run(args):
        failures, median_ms, p95_ms = next(results)
        assert args.url == "http://127.0.0.1:8765/item/123"
        assert args.threshold_ms == 10.0
        return benchmark.BenchmarkSummary(
            mode=args.mode,
            start_url=args.url,
            steps_requested=args.steps,
            steps_measured=args.steps,
            warmup=args.warmup,
            threshold_ms=args.threshold_ms,
            threshold_failures=failures,
            min_ms=median_ms,
            median_ms=median_ms,
            mean_ms=median_ms,
            p90_ms=p95_ms,
            p95_ms=p95_ms,
            max_ms=p95_ms,
            steps=[],
        )

    monkeypatch.setattr(benchmark, "run_benchmark", fake_run)
    args = benchmark.parse_args(
        [
            "--suite",
            str(suite_path),
            "--repeat",
            "3",
            "--min-failures",
            "1",
            "--max-failures",
            "1",
        ]
    )

    summary = benchmark.run_suite_benchmark(args)

    assert summary.passed is True
    assert summary.cases[0].best_run_index == 2
    assert [run.threshold_failures for run in summary.cases[0].runs] == [2, 1, 1]


def test_suite_main_writes_all_runs_and_returns_one_for_failed_case(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    benchmark = load_benchmark_module()
    suite_path = tmp_path / "suite.json"
    output_path = tmp_path / "result.json"
    suite_path.write_text(
        json.dumps(
            [
                {
                    "name": "filtersok-video",
                    "url": "http://127.0.0.1:8765/filter/type%3Avideo/item/123",
                    "threshold_ms": 15,
                }
            ]
        ),
        encoding="utf-8",
    )

    def fake_run(args):
        return benchmark.BenchmarkSummary(
            mode=args.mode,
            start_url=args.url,
            steps_requested=2,
            steps_measured=2,
            warmup=args.warmup,
            threshold_ms=args.threshold_ms,
            threshold_failures=2,
            min_ms=10.0,
            median_ms=20.0,
            mean_ms=20.0,
            p90_ms=30.0,
            p95_ms=30.0,
            max_ms=30.0,
            steps=[],
        )

    monkeypatch.setattr(benchmark, "run_benchmark", fake_run)

    exit_code = benchmark.main(
        [
            "--suite",
            str(suite_path),
            "--repeat",
            "2",
            "--max-failures",
            "1",
            "--json-output",
            str(output_path),
        ]
    )

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "filtersok-video:" in output
    assert "brudd/run=[2, 2] — FEIL" in output
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["passed"] is False
    assert payload["cases"][0]["best_run_index"] == 0
    assert len(payload["cases"][0]["runs"]) == 2
    assert payload["cases"][0]["best_run"]["threshold_failures"] == 2


def test_suite_rejects_invalid_case_as_runtime_error(tmp_path: Path, capsys) -> None:
    benchmark = load_benchmark_module()
    suite_path = tmp_path / "suite.json"
    suite_path.write_text('[{"name": "mangler terskel", "url": "http://127.0.0.1/"}]', encoding="utf-8")

    exit_code = benchmark.main(["--suite", str(suite_path)])

    assert exit_code == 2
    assert "threshold_ms" in capsys.readouterr().err
