#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.client
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_URL = "http://127.0.0.1:8765/"
BENCHMARK_HEADER = "X-Bildebank-Benchmark"
SERVER_TIMING_STEP_ORDER = (
    "parse",
    "db_connect",
    "browser_item_order",
    "item_by_id",
    "adjacent",
    "month_nav",
    "source_item_page_html",
    "encode/respond_before_write",
    "hotkey_read_payload",
    "hotkey_validate",
    "hotkey_filter_parse",
    "hotkey_filter_before",
    "hotkey_apply",
    "hotkey_post_apply",
    "hotkey_filter_after",
    "hotkey_redirect",
    "tag_read_payload",
    "tag_validate",
    "tag_apply",
    "total",
)


@dataclass(frozen=True)
class StepResult:
    index: int
    elapsed_ms: float
    url: str
    first_byte_ms: float | None = None
    read_ms: float | None = None
    body_bytes: int | None = None
    server_ms: float | None = None
    server_timing_ms: dict[str, float] | None = None


@dataclass(frozen=True)
class BenchmarkSummary:
    mode: str
    start_url: str
    steps_requested: int
    steps_measured: int
    warmup: int
    threshold_ms: float | None
    threshold_failures: int
    min_ms: float | None
    median_ms: float | None
    mean_ms: float | None
    p90_ms: float | None
    p95_ms: float | None
    max_ms: float | None
    steps: list[StepResult]


@dataclass(frozen=True)
class ProfileStepResult:
    index: int
    url: str
    total_ms: float
    connect_ms: float
    item_ms: float
    adjacent_ms: float
    month_nav_ms: float
    html_ms: float
    html_bytes: int


@dataclass(frozen=True)
class ProfileSummary:
    mode: str
    start_url: str
    target: str
    steps_requested: int
    steps_measured: int
    warmup: int
    threshold_ms: float | None
    threshold_failures: int
    total: dict[str, float | None]
    connect: dict[str, float | None]
    item: dict[str, float | None]
    adjacent: dict[str, float | None]
    month_nav: dict[str, float | None]
    html: dict[str, float | None]
    steps: list[ProfileStepResult]


@dataclass(frozen=True)
class YearsProfileStepResult:
    index: int
    total_ms: float
    year_cards_ms: float
    year_summaries_ms: float
    thumbnail_items_ms: float
    card_html_ms: float
    controls_ms: float
    shell_ms: float
    html_bytes: int
    year_count: int


@dataclass(frozen=True)
class YearsProfileSummary:
    mode: str
    target: str
    steps_requested: int
    steps_measured: int
    warmup: int
    threshold_ms: float | None
    threshold_failures: int
    total: dict[str, float | None]
    year_cards: dict[str, float | None]
    year_summaries: dict[str, float | None]
    thumbnail_items: dict[str, float | None]
    card_html: dict[str, float | None]
    controls: dict[str, float | None]
    shell: dict[str, float | None]
    html_bytes_median: float | None
    year_count_median: float | None
    steps: list[YearsProfileStepResult]


BenchmarkResult = BenchmarkSummary | ProfileSummary | YearsProfileSummary


@dataclass(frozen=True)
class SuiteCase:
    name: str
    url: str
    threshold_ms: float


@dataclass(frozen=True)
class SuiteCaseResult:
    name: str
    url: str
    threshold_ms: float
    runs: list[BenchmarkResult]
    best_run_index: int
    passed: bool

    @property
    def best_run(self) -> BenchmarkResult:
        return self.runs[self.best_run_index]


@dataclass(frozen=True)
class SuiteSummary:
    suite_path: str
    mode: str
    repeat: int
    min_failures: int
    max_failures: int
    passed: bool
    cases: list[SuiteCaseResult]


class NextLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.next_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a" or self.next_href is not None:
            return
        attr = dict(attrs)
        if attr.get("data-key-nav") == "next" and attr.get("href"):
            self.next_href = attr["href"]


class ItemPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.csrf_token: str | None = None
        self.file_id: int | None = None
        self.source_url: str | None = None
        self.tags: list[dict[str, object]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "meta" and attr.get("name") == "csrf-token" and attr.get("content"):
            self.csrf_token = attr["content"]
        if attr.get("data-browser-item-id") and self.file_id is None:
            try:
                self.file_id = int(str(attr["data-browser-item-id"]))
            except ValueError:
                pass
            self.source_url = attr.get("data-browser-source-url")
        if tag == "button" and attr.get("data-tag-toggle") and attr.get("data-tag-name"):
            try:
                file_id = int(str(attr["data-tag-toggle"]))
            except ValueError:
                return
            pressed = attr.get("aria-pressed") == "true"
            self.tags.append({"file_id": file_id, "tag_name": attr["data-tag-name"], "tagged": not pressed})


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.suite is not None:
            suite_summary = run_suite_benchmark(args)
        else:
            summary = run_benchmark(args)
    except (OSError, RuntimeError) as exc:
        print(f"FEIL: {exc}", file=sys.stderr)
        return 2

    if args.suite is not None:
        print_suite_summary(suite_summary, show_server_timing=args.suite_server_timing)
        try:
            if args.json_output:
                write_json_output(args.json_output, suite_summary_to_json(suite_summary))
        except OSError as exc:
            print(f"FEIL: {exc}", file=sys.stderr)
            return 2
        return 0 if suite_summary.passed else 1

    if isinstance(summary, YearsProfileSummary):
        print_years_profile_summary(summary)
    elif isinstance(summary, ProfileSummary):
        print_profile_summary(summary)
    else:
        print_summary(summary)
    try:
        if args.json_output:
            write_json_output(args.json_output, summary_to_json(summary))
    except OSError as exc:
        print(f"FEIL: {exc}", file=sys.stderr)
        return 2
    if summary.threshold_failures:
        return 1
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Mål bla-ytelse i bildebrowseren. Start først `bildebank run-server`, "
            "og kjør dette scriptet mot serveradressen."
        )
    )
    parser.add_argument("--url", default=DEFAULT_URL, help=f"Startside eller item-URL. Standard: {DEFAULT_URL}")
    parser.add_argument("--suite", type=Path, help="JSON-fil med benchmark-cases.")
    parser.add_argument(
        "--suite-server-timing",
        action="store_true",
        help="Vis detaljerte Server-Timing-steg for beste run i hver suite-case.",
    )
    parser.add_argument("--repeat", type=positive_int, default=3, help="Kjør hver suite-case N ganger. Standard: 3")
    parser.add_argument(
        "--min-failures",
        type=non_negative_int,
        default=0,
        help="Minste tillatte antall terskelbrudd for beste suite-kjøring. Standard: 0",
    )
    parser.add_argument(
        "--max-failures",
        type=non_negative_int,
        default=5,
        help="Største tillatte antall terskelbrudd for beste suite-kjøring. Standard: 5",
    )
    parser.add_argument(
        "--mode",
        choices=("browser", "server", "server-keepalive", "profile", "years-profile", "hotkey", "tag"),
        default="browser",
    )
    parser.add_argument("--target", type=Path, help="Bildesamlingsmappe. Kreves for --mode profile og years-profile.")
    parser.add_argument("--steps", type=positive_int, default=50, help="Antall neste-klikk som måles. Standard: 50")
    parser.add_argument("--warmup", type=non_negative_int, default=5, help="Antall oppvarmingsklikk før måling. Standard: 5")
    parser.add_argument("--hotkey", default="1", help="Hurtigtast som måles med --mode hotkey. Standard: 1")
    parser.add_argument("--tag", help="Taggnavn som måles med --mode tag. Standard: første taggknapp på siden.")
    parser.add_argument("--threshold-ms", type=float, help="Returner exit code 1 hvis ett eller flere steg er tregere.")
    parser.add_argument("--json-output", help="Skriv rådata og oppsummering til JSON-fil.")
    parser.add_argument("--timeout-ms", type=positive_int, default=10_000, help="Timeout per side/klikk. Standard: 10000")
    parser.add_argument("--headed", action="store_true", help="Vis nettleservinduet i browser-modus.")
    parser.add_argument(
        "--wait",
        choices=("domcontentloaded", "load", "media"),
        default="load",
        help="Hvor lenge browser-modus venter etter hvert klikk. Standard: load",
    )
    return parser.parse_args(argv)


def run_benchmark(args: argparse.Namespace) -> BenchmarkResult:
    if args.mode == "years-profile":
        return run_years_profile_benchmark(args)
    if args.mode == "profile":
        return run_profile_benchmark(args)
    if args.mode == "hotkey":
        return run_hotkey_benchmark(args)
    if args.mode == "tag":
        return run_tag_benchmark(args)
    if args.mode == "browser":
        return run_browser_benchmark(args)
    if args.mode == "server-keepalive":
        return run_server_keepalive_benchmark(args)
    return run_server_benchmark(args)


def run_suite_benchmark(args: argparse.Namespace) -> SuiteSummary:
    if args.suite is None:
        raise RuntimeError("--suite mangler.")
    if args.min_failures > args.max_failures:
        raise RuntimeError("--min-failures kan ikke være større enn --max-failures.")
    cases = load_suite_cases(args.suite)
    case_results: list[SuiteCaseResult] = []
    for case in cases:
        runs: list[BenchmarkResult] = []
        for _ in range(args.repeat):
            run_args = argparse.Namespace(**vars(args))
            run_args.url = case.url
            run_args.threshold_ms = case.threshold_ms
            runs.append(run_benchmark(run_args))
        best_run_index = min(range(len(runs)), key=lambda index: summary_sort_key(runs[index]))
        best_run = runs[best_run_index]
        passed = args.min_failures <= best_run.threshold_failures <= args.max_failures
        case_results.append(
            SuiteCaseResult(
                name=case.name,
                url=case.url,
                threshold_ms=case.threshold_ms,
                runs=runs,
                best_run_index=best_run_index,
                passed=passed,
            )
        )
    return SuiteSummary(
        suite_path=str(args.suite),
        mode=args.mode,
        repeat=args.repeat,
        min_failures=args.min_failures,
        max_failures=args.max_failures,
        passed=all(case.passed for case in case_results),
        cases=case_results,
    )


def load_suite_cases(path: Path) -> list[SuiteCase]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Suite-filen inneholder ugyldig JSON: {path}: {exc}") from exc
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("Suite-filen må inneholde en ikke-tom JSON-liste.")
    cases: list[SuiteCase] = []
    for index, raw_case in enumerate(payload, start=1):
        if not isinstance(raw_case, dict):
            raise RuntimeError(f"Suite-case #{index} må være et JSON-objekt.")
        name = raw_case.get("name")
        url = raw_case.get("url")
        threshold_ms = raw_case.get("threshold_ms")
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError(f"Suite-case #{index} mangler gyldig name.")
        if not isinstance(url, str) or not url.strip():
            raise RuntimeError(f"Suite-case {name!r} mangler gyldig url.")
        if isinstance(threshold_ms, bool) or not isinstance(threshold_ms, (int, float)):
            raise RuntimeError(f"Suite-case {name!r} mangler numerisk threshold_ms.")
        if threshold_ms < 0:
            raise RuntimeError(f"Suite-case {name!r} har negativ threshold_ms.")
        cases.append(SuiteCase(name=name.strip(), url=url.strip(), threshold_ms=float(threshold_ms)))
    return cases


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("må være større enn 0")
    return number


def non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("må være 0 eller større")
    return number


def run_browser_benchmark(args: argparse.Namespace) -> BenchmarkSummary:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright er ikke installert. Installer med "
            "`.venv/bin/pip install playwright` og kjør "
            "`.venv/bin/python -m playwright install chromium`, "
            "eller bruk `--mode server` for HTTP-only måling."
        ) from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed)
        page = browser.new_page()
        try:
            page.goto(args.url, wait_until=args.wait if args.wait != "media" else "load", timeout=args.timeout_ms)
            wait_for_media(page, args.timeout_ms)
            for _ in range(args.warmup):
                click_next(page, args.timeout_ms, args.wait)
            steps: list[StepResult] = []
            for index in range(1, args.steps + 1):
                try:
                    elapsed_ms = click_next(page, args.timeout_ms, args.wait)
                except RuntimeError:
                    break
                steps.append(StepResult(index=index, elapsed_ms=elapsed_ms, url=page.url))
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(f"Timeout i nettleser: {exc}") from exc
        finally:
            browser.close()
    return build_summary("browser", args, steps)


def click_next(page: Any, timeout_ms: int, wait_mode: str) -> float:
    next_link = page.locator('a[data-key-nav="next"]').first
    if next_link.count() == 0:
        raise RuntimeError("Fant ingen aktiv Neste bilde-lenke.")
    start = time.perf_counter()
    with page.expect_navigation(wait_until=wait_mode if wait_mode != "media" else "load", timeout=timeout_ms):
        next_link.click()
    if wait_mode == "media":
        wait_for_media(page, timeout_ms)
    return (time.perf_counter() - start) * 1000.0


def wait_for_media(page: Any, timeout_ms: int) -> None:
    page.wait_for_function(
        """
        () => {
          const media = document.querySelector('.stage img, .stage video');
          if (!media) return true;
          if (media.tagName === 'IMG') return media.complete && media.naturalWidth > 0;
          return media.readyState >= 1;
        }
        """,
        timeout=timeout_ms,
    )


def run_server_benchmark(args: argparse.Namespace) -> BenchmarkSummary:
    url = args.url
    html = fetch_text(url, args.timeout_ms)
    for _ in range(args.warmup):
        _elapsed_ms, url, html = fetch_next_page(url, html, args.timeout_ms)
    steps: list[StepResult] = []
    for index in range(1, args.steps + 1):
        try:
            elapsed_ms, url, html = fetch_next_page(url, html, args.timeout_ms)
        except RuntimeError:
            break
        steps.append(StepResult(index=index, elapsed_ms=elapsed_ms, url=url))
    return build_summary("server", args, steps)


def run_server_keepalive_benchmark(args: argparse.Namespace) -> BenchmarkSummary:
    parsed = urllib.parse.urlparse(args.url)
    if parsed.scheme != "http" or not parsed.hostname:
        raise RuntimeError("--mode server-keepalive støtter foreløpig bare http-URL-er.")
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=args.timeout_ms / 1000.0)
    try:
        url = args.url
        html, _first_byte_ms, _read_ms, _body_bytes, _server_ms, _server_timing = fetch_text_keepalive_timed(conn, url)
        for _ in range(args.warmup):
            _step, url, html = fetch_next_page_keepalive_step(conn, url, html, 0)
        steps: list[StepResult] = []
        for index in range(1, args.steps + 1):
            try:
                step, url, html = fetch_next_page_keepalive_step(conn, url, html, index)
            except RuntimeError:
                break
            steps.append(step)
        return build_summary("server-keepalive", args, steps)
    finally:
        conn.close()


def run_hotkey_benchmark(args: argparse.Namespace) -> BenchmarkSummary:
    html = fetch_text(args.url, args.timeout_ms)
    item_page = parse_item_page(html)
    if item_page.file_id is None:
        raise RuntimeError("Fant ikke data-browser-item-id på startsiden.")
    if not item_page.csrf_token:
        raise RuntimeError("Fant ikke CSRF-token på startsiden.")
    for _ in range(args.warmup):
        post_hotkey_step(args.url, item_page, args.hotkey, 0, args.timeout_ms)
    steps: list[StepResult] = []
    for index in range(1, args.steps + 1):
        steps.append(post_hotkey_step(args.url, item_page, args.hotkey, index, args.timeout_ms))
    return build_summary("hotkey", args, steps)


def run_tag_benchmark(args: argparse.Namespace) -> BenchmarkSummary:
    html = fetch_text(args.url, args.timeout_ms)
    item_page = parse_item_page(html)
    if not item_page.csrf_token:
        raise RuntimeError("Fant ikke CSRF-token på startsiden.")
    tag_payload = select_tag_payload(item_page, args.tag)
    for _ in range(args.warmup):
        post_tag_step(args.url, item_page.csrf_token, tag_payload, 0, args.timeout_ms)
    steps: list[StepResult] = []
    for index in range(1, args.steps + 1):
        steps.append(post_tag_step(args.url, item_page.csrf_token, tag_payload, index, args.timeout_ms))
    return build_summary("tag", args, steps)


def select_tag_payload(item_page: ItemPageParser, tag_name: str | None) -> dict[str, object]:
    if not item_page.tags:
        raise RuntimeError("Fant ingen taggknapper på startsiden.")
    for tag in item_page.tags:
        if tag_name is None or tag.get("tag_name") == tag_name:
            return dict(tag)
    raise RuntimeError(f"Fant ikke taggknapp for {tag_name!r} på startsiden.")


def parse_item_page(html: str) -> ItemPageParser:
    parser = ItemPageParser()
    parser.feed(html)
    return parser


def post_hotkey_step(
    url: str,
    item_page: ItemPageParser,
    key: str,
    index: int,
    timeout_ms: int,
) -> StepResult:
    payload: dict[str, object] = {"file_id": item_page.file_id, "key": key}
    if item_page.source_url:
        payload["source_url"] = item_page.source_url
    return post_json_api_step(
        url,
        "/api/item-hotkey-action",
        item_page.csrf_token or "",
        payload,
        index,
        timeout_ms,
    )


def post_tag_step(
    url: str,
    csrf_token: str,
    payload: dict[str, object],
    index: int,
    timeout_ms: int,
) -> StepResult:
    return post_json_api_step(
        url,
        "/api/item-tag",
        csrf_token,
        payload,
        index,
        timeout_ms,
    )


def post_json_api_step(
    url: str,
    path: str,
    csrf_token: str,
    payload: dict[str, object],
    index: int,
    timeout_ms: int,
) -> StepResult:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        urllib.parse.urljoin(url, path),
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf_token,
            BENCHMARK_HEADER: "1",
        },
        method="POST",
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout_ms / 1000.0) as response:
            first_byte_ms = elapsed_ms(start)
            if response.status >= 400:
                raise RuntimeError(f"HTTP {response.status} fra {path}")
            server_ms = (
                float(response.headers["X-Bildebank-Request-Ms"])
                if "X-Bildebank-Request-Ms" in response.headers
                else None
            )
            server_timing = parse_server_timing_header(response.headers.get("Server-Timing", ""))
            read_start = time.perf_counter()
            content = response.read()
            read_ms = elapsed_ms(read_start)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} fra {path}: {detail}") from exc
    total_ms = elapsed_ms(start)
    return StepResult(
        index=index,
        elapsed_ms=total_ms,
        url=url,
        first_byte_ms=first_byte_ms,
        read_ms=read_ms,
        body_bytes=len(content),
        server_ms=server_ms,
        server_timing_ms=server_timing,
    )


def run_profile_benchmark(args: argparse.Namespace) -> ProfileSummary:
    from bildebank import db
    from bildebank.config import load_config

    if args.target is None:
        raise RuntimeError("--target må oppgis med --mode profile.")
    target = args.target
    if not target.exists() or not target.is_dir():
        raise RuntimeError(f"Bildesamlingen finnes ikke som mappe: {target}")
    db.prepare_database(target)
    config = load_config(Path(__file__).resolve().parents[1])

    source, file_id = profile_source_and_file_id(target, args.url)
    month_keys = profile_cached_month_keys(target, source)
    item_ids = profile_cached_item_ids(target, source)
    item_positions = profile_item_positions(item_ids)
    current_file_id = file_id
    for _ in range(args.warmup):
        step = profile_item_step(
            target,
            source,
            current_file_id,
            0,
            month_keys=month_keys,
            item_ids=item_ids,
            item_positions=item_positions,
            config=config,
        )
        next_file_id = next_profile_file_id(step.url)
        if next_file_id is None:
            break
        current_file_id = next_file_id

    steps: list[ProfileStepResult] = []
    for index in range(1, args.steps + 1):
        step = profile_item_step(
            target,
            source,
            current_file_id,
            index,
            month_keys=month_keys,
            item_ids=item_ids,
            item_positions=item_positions,
            config=config,
        )
        steps.append(step)
        next_file_id = next_profile_file_id(step.url)
        if next_file_id is None:
            break
        current_file_id = next_file_id
    return build_profile_summary(args, steps)


def run_years_profile_benchmark(args: argparse.Namespace) -> YearsProfileSummary:
    from bildebank import db
    from bildebank.config import load_config

    if args.target is None:
        raise RuntimeError("--target må oppgis med --mode years-profile.")
    target = args.target
    if not target.exists() or not target.is_dir():
        raise RuntimeError(f"Bildesamlingen finnes ikke som mappe: {target}")
    db.prepare_database(target)
    config = load_config(Path(__file__).resolve().parents[1])

    for _ in range(args.warmup):
        years_profile_step(target, 0, config=config)

    steps = [years_profile_step(target, index, config=config) for index in range(1, args.steps + 1)]
    return build_years_profile_summary(args, steps)


def years_profile_step(target: Path, index: int, *, config: Any | None = None) -> YearsProfileStepResult:
    from bildebank.server_browser_overview_html import (
        year_card_html,
        years_navigation_controls_html,
    )
    from bildebank.server_browser_queries import (
        browser_year_summaries,
        items_by_file_ids,
    )
    from bildebank.server_pages import shell_page_html

    hide_out_of_focus = bool(config.browser.hide_out_of_focus) if config is not None else False
    face_enabled = bool(config.face_recognition.enabled) if config is not None else True
    openclip_enabled = bool(config.openclip.enabled) if config is not None else True

    total_start = time.perf_counter()

    start = time.perf_counter()
    summaries = browser_year_summaries(target, hide_out_of_focus=hide_out_of_focus)
    year_summaries_ms = elapsed_ms(start)

    start = time.perf_counter()
    items = {
        int(item["id"]): item
        for item in items_by_file_ids(
            target,
            [int(summary["item_id"]) for summary in summaries],
            hide_out_of_focus=hide_out_of_focus,
        )
    }
    thumbnail_items_ms = elapsed_ms(start)

    start = time.perf_counter()
    year_cards = [
        {
            "year": str(summary["year"]),
            "month_count": int(summary["month_count"]),
            "item_count": int(summary["item_count"]),
            "first_month": str(summary["first_month"]),
            "item": item,
        }
        for summary in summaries
        if (item := items.get(int(summary["item_id"]))) is not None
    ]
    year_cards_ms = year_summaries_ms + thumbnail_items_ms + elapsed_ms(start)

    start = time.perf_counter()
    cards = "\n".join(year_card_html(target, card) for card in year_cards)
    content = cards if cards else '<p class="meta">Ingen filer i bildesamlingen.</p>'
    card_html_ms = elapsed_ms(start)

    start = time.perf_counter()
    controls = years_navigation_controls_html(year_cards)
    controls_ms = elapsed_ms(start)

    start = time.perf_counter()
    html = shell_page_html(
        "År",
        f"""
        <h1>År</h1>
        {controls}
        <section class="month-grid-server">{content}</section>
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )
    shell_ms = elapsed_ms(start)

    return YearsProfileStepResult(
        index=index,
        total_ms=elapsed_ms(total_start),
        year_cards_ms=year_cards_ms,
        year_summaries_ms=year_summaries_ms,
        thumbnail_items_ms=thumbnail_items_ms,
        card_html_ms=card_html_ms,
        controls_ms=controls_ms,
        shell_ms=shell_ms,
        html_bytes=len(html.encode("utf-8")),
        year_count=len(year_cards),
    )


def profile_source_and_file_id(target: Path, url: str) -> tuple[Any, int]:
    from bildebank.server_browser_queries import imported_source_by_id
    from bildebank.server_browser_sources import (
        all_browser_source,
        imported_source_browser_source,
        parse_source_path,
    )
    from bildebank.server_filter import text_filter_browser_source

    parsed = urllib.parse.urlparse(url)
    path = parsed.path
    if path.startswith("/item/"):
        return all_browser_source(), int(path.removeprefix("/item/").strip("/"))
    if path.startswith("/filter/"):
        raw_query, page_mode, raw_value = parse_source_path(path.removeprefix("/filter/"))
        if page_mode != "item":
            raise RuntimeError("Profile-modus trenger en item-URL, for eksempel /filter/type%3Avideo/item/123.")
        query = urllib.parse.unquote(raw_query).strip()
        return text_filter_browser_source(query, target), int(raw_value)
    if path.startswith("/source/"):
        raw_source_id, page_mode, raw_value = parse_source_path(path.removeprefix("/source/"))
        if page_mode != "item":
            raise RuntimeError("Profile-modus trenger en item-URL, for eksempel /source/7/item/123.")
        try:
            source_id = int(urllib.parse.unquote(raw_source_id).strip())
        except ValueError as exc:
            raise RuntimeError(f"Ugyldig kilde-ID: {raw_source_id}") from exc
        imported_source = imported_source_by_id(target, source_id)
        if imported_source is None:
            raise RuntimeError(f"Fant ikke kilde #{source_id}.")
        return imported_source_browser_source(imported_source), int(raw_value)
    raise RuntimeError(
        "Profile-modus støtter foreløpig /item/<id>, /filter/<query>/item/<id> "
        "og /source/<id>/item/<id>."
    )


def profile_cached_month_keys(target: Path, source: Any) -> list[str] | None:
    from bildebank.server_browser_queries import browser_month_keys, source_month_keys
    from bildebank.server_browser_sources import all_browser_source, source_has_sql_filter

    if source == all_browser_source():
        return browser_month_keys(target)
    if source_has_sql_filter(source):
        return source_month_keys(target, source)
    return None


def profile_cached_item_ids(target: Path, source: Any) -> list[int] | None:
    from bildebank.server_browser_queries import browser_item_ids, source_item_ids
    from bildebank.server_browser_sources import all_browser_source, source_has_sql_filter

    if source == all_browser_source():
        return browser_item_ids(target)
    if source_has_sql_filter(source):
        return source_item_ids(target, source)
    return None


def profile_item_positions(item_ids: list[int] | None) -> dict[int, int] | None:
    if item_ids is None:
        return None
    return {file_id: index for index, file_id in enumerate(item_ids)}


def profile_item_step(
    target: Path,
    source: Any,
    file_id: int,
    index: int,
    *,
    month_keys: list[str] | None = None,
    item_ids: list[int] | None = None,
    item_positions: dict[int, int] | None = None,
    config: Any | None = None,
) -> ProfileStepResult:
    from bildebank import db
    from bildebank.server_browser_queries import (
        adjacent_items_from_id_order,
        adjacent_source_items,
        item_by_id,
        month_key_for_item,
        month_navigation_for_keys,
        source_item_by_id,
        source_month_navigation,
    )
    from bildebank.server_browser_sources import source_item_url
    from bildebank.server_pages import source_item_page_html

    total_start = time.perf_counter()
    start = time.perf_counter()
    conn = db.connect(target)
    connect_ms = elapsed_ms(start)
    try:
        start = time.perf_counter()
        if item_ids is None:
            item = source_item_by_id(target, source, file_id, conn=conn)
        else:
            item = item_by_id(target, file_id, conn=conn) if file_id in (item_positions or {}) else None
        item_ms = elapsed_ms(start)
        if item is None:
            raise RuntimeError(f"Fant ikke item #{file_id} i profilert utvalg.")

        start = time.perf_counter()
        if item_ids is None:
            previous_item, next_item = adjacent_source_items(target, source, item, conn=conn)
        else:
            previous_item, next_item = adjacent_items_from_id_order(item_ids, int(item["id"]), item_positions)
        adjacent_ms = elapsed_ms(start)

        start = time.perf_counter()
        if month_keys is None:
            month_nav = source_month_navigation(target, source, item, conn=conn)
        else:
            month_nav = month_navigation_for_keys(month_keys, month_key_for_item(target, item))
        month_nav_ms = elapsed_ms(start)

        start = time.perf_counter()
        kwargs: dict[str, Any] = {"conn": conn}
        if config is not None:
            kwargs.update(
                {
                    "face_enabled": config.face_recognition.enabled,
                    "openclip_enabled": config.openclip.enabled,
                    "face_config": config.face_recognition,
                    "hide_out_of_focus": config.browser.hide_out_of_focus,
                }
            )
        html = source_item_page_html(target, source, item, previous_item, next_item, month_nav, **kwargs)
        html_ms = elapsed_ms(start)
        total_ms = elapsed_ms(total_start)
    finally:
        conn.close()

    next_url = source_item_url(source, int(next_item["id"])) if next_item is not None else source_item_url(source, file_id)
    return ProfileStepResult(
        index=index,
        url=next_url,
        total_ms=total_ms,
        connect_ms=connect_ms,
        item_ms=item_ms,
        adjacent_ms=adjacent_ms,
        month_nav_ms=month_nav_ms,
        html_ms=html_ms,
        html_bytes=len(html.encode("utf-8")),
    )


def next_profile_file_id(url: str) -> int | None:
    parsed = urllib.parse.urlparse(url)
    if "/item/" not in parsed.path:
        return None
    raw_id = parsed.path.rsplit("/item/", 1)[1].strip("/")
    return int(raw_id) if raw_id.isdigit() else None


def elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def fetch_next_page(url: str, html: str, timeout_ms: int) -> tuple[float, str, str]:
    parser = NextLinkParser()
    parser.feed(html)
    if parser.next_href is None:
        raise RuntimeError("Fant ingen aktiv Neste bilde-lenke.")
    next_url = urllib.parse.urljoin(url, parser.next_href)
    start = time.perf_counter()
    next_html = fetch_text(next_url, timeout_ms)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    if not next_html:
        raise RuntimeError(f"Tom respons fra {next_url}")
    return elapsed_ms, next_url, next_html


def fetch_next_page_keepalive_step(
    conn: http.client.HTTPConnection,
    url: str,
    html: str,
    index: int,
) -> tuple[StepResult, str, str]:
    parser = NextLinkParser()
    parser.feed(html)
    if parser.next_href is None:
        raise RuntimeError("Fant ingen aktiv Neste bilde-lenke.")
    next_url = urllib.parse.urljoin(url, parser.next_href)
    start = time.perf_counter()
    next_html, first_byte_ms, read_ms, body_bytes, server_ms, server_timing = fetch_text_keepalive_timed(conn, next_url)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    if not next_html:
        raise RuntimeError(f"Tom respons fra {next_url}")
    return (
        StepResult(
            index=index,
            elapsed_ms=elapsed_ms,
            url=next_url,
            first_byte_ms=first_byte_ms,
            read_ms=read_ms,
            body_bytes=body_bytes,
            server_ms=server_ms,
            server_timing_ms=server_timing,
        ),
        next_url,
        next_html,
    )


def fetch_text(url: str, timeout_ms: int) -> str:
    with urllib.request.urlopen(url, timeout=timeout_ms / 1000.0) as response:
        encoding = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(encoding, errors="replace")


def fetch_text_keepalive(conn: http.client.HTTPConnection, url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"
    conn.request("GET", path)
    response = conn.getresponse()
    try:
        if response.status >= 400:
            raise RuntimeError(f"HTTP {response.status} fra {url}")
        content_type = response.headers.get("Content-Type", "")
        encoding = "utf-8"
        if "charset=" in content_type:
            encoding = content_type.rsplit("charset=", 1)[1].split(";", 1)[0].strip() or encoding
        return response.read().decode(encoding, errors="replace")
    finally:
        response.close()


def fetch_text_keepalive_timed(
    conn: http.client.HTTPConnection,
    url: str,
) -> tuple[str, float, float, int, float | None, dict[str, float]]:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"
    start = time.perf_counter()
    conn.request("GET", path, headers={BENCHMARK_HEADER: "1"})
    response = conn.getresponse()
    first_byte_ms = elapsed_ms(start)
    try:
        if response.status >= 400:
            raise RuntimeError(f"HTTP {response.status} fra {url}")
        server_ms = float(response.headers["X-Bildebank-Request-Ms"]) if "X-Bildebank-Request-Ms" in response.headers else None
        server_timing = parse_server_timing_header(response.headers.get("Server-Timing", ""))
        content_type = response.headers.get("Content-Type", "")
        encoding = "utf-8"
        if "charset=" in content_type:
            encoding = content_type.rsplit("charset=", 1)[1].split(";", 1)[0].strip() or encoding
        start = time.perf_counter()
        content = response.read()
        read_ms = elapsed_ms(start)
        return content.decode(encoding, errors="replace"), first_byte_ms, read_ms, len(content), server_ms, server_timing
    finally:
        response.close()


def parse_server_timing_header(value: str) -> dict[str, float]:
    timings: dict[str, float] = {}
    for entry in value.split(","):
        parts = [part.strip() for part in entry.split(";") if part.strip()]
        if not parts:
            continue
        name = parts[0]
        for part in parts[1:]:
            if not part.startswith("dur="):
                continue
            raw_duration = part.removeprefix("dur=").strip().strip('"')
            try:
                timings[name] = float(raw_duration)
            except ValueError:
                pass
            break
    return timings


def build_summary(mode: str, args: argparse.Namespace, steps: list[StepResult]) -> BenchmarkSummary:
    values = [step.elapsed_ms for step in steps]
    threshold_failures = (
        sum(1 for value in values if args.threshold_ms is not None and value > args.threshold_ms)
        if args.threshold_ms is not None
        else 0
    )
    return BenchmarkSummary(
        mode=mode,
        start_url=args.url,
        steps_requested=args.steps,
        steps_measured=len(steps),
        warmup=args.warmup,
        threshold_ms=args.threshold_ms,
        threshold_failures=threshold_failures,
        min_ms=min(values) if values else None,
        median_ms=statistics.median(values) if values else None,
        mean_ms=statistics.fmean(values) if values else None,
        p90_ms=percentile(values, 90),
        p95_ms=percentile(values, 95),
        max_ms=max(values) if values else None,
        steps=steps,
    )


def build_profile_summary(args: argparse.Namespace, steps: list[ProfileStepResult]) -> ProfileSummary:
    total_values = [step.total_ms for step in steps]
    threshold_failures = (
        sum(1 for value in total_values if args.threshold_ms is not None and value > args.threshold_ms)
        if args.threshold_ms is not None
        else 0
    )
    return ProfileSummary(
        mode="profile",
        start_url=args.url,
        target=str(args.target),
        steps_requested=args.steps,
        steps_measured=len(steps),
        warmup=args.warmup,
        threshold_ms=args.threshold_ms,
        threshold_failures=threshold_failures,
        total=stats_dict(total_values),
        connect=stats_dict([step.connect_ms for step in steps]),
        item=stats_dict([step.item_ms for step in steps]),
        adjacent=stats_dict([step.adjacent_ms for step in steps]),
        month_nav=stats_dict([step.month_nav_ms for step in steps]),
        html=stats_dict([step.html_ms for step in steps]),
        steps=steps,
    )


def build_years_profile_summary(args: argparse.Namespace, steps: list[YearsProfileStepResult]) -> YearsProfileSummary:
    total_values = [step.total_ms for step in steps]
    threshold_failures = (
        sum(1 for value in total_values if args.threshold_ms is not None and value > args.threshold_ms)
        if args.threshold_ms is not None
        else 0
    )
    return YearsProfileSummary(
        mode="years-profile",
        target=str(args.target),
        steps_requested=args.steps,
        steps_measured=len(steps),
        warmup=args.warmup,
        threshold_ms=args.threshold_ms,
        threshold_failures=threshold_failures,
        total=stats_dict(total_values),
        year_cards=stats_dict([step.year_cards_ms for step in steps]),
        year_summaries=stats_dict([step.year_summaries_ms for step in steps]),
        thumbnail_items=stats_dict([step.thumbnail_items_ms for step in steps]),
        card_html=stats_dict([step.card_html_ms for step in steps]),
        controls=stats_dict([step.controls_ms for step in steps]),
        shell=stats_dict([step.shell_ms for step in steps]),
        html_bytes_median=statistics.median([step.html_bytes for step in steps]) if steps else None,
        year_count_median=statistics.median([step.year_count for step in steps]) if steps else None,
        steps=steps,
    )


def stats_dict(values: list[float]) -> dict[str, float | None]:
    return {
        "min_ms": min(values) if values else None,
        "median_ms": statistics.median(values) if values else None,
        "mean_ms": statistics.fmean(values) if values else None,
        "p90_ms": percentile(values, 90),
        "p95_ms": percentile(values, 95),
        "max_ms": max(values) if values else None,
    }


def percentile(values: list[float], percent: int) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    sorted_values = sorted(values)
    index = round((percent / 100) * (len(sorted_values) - 1))
    return sorted_values[index]


def summary_median_ms(summary: BenchmarkResult) -> float | None:
    if isinstance(summary, YearsProfileSummary):
        return summary.total["median_ms"]
    if isinstance(summary, ProfileSummary):
        return summary.total["median_ms"]
    return summary.median_ms


def summary_p95_ms(summary: BenchmarkResult) -> float | None:
    if isinstance(summary, YearsProfileSummary):
        return summary.total["p95_ms"]
    if isinstance(summary, ProfileSummary):
        return summary.total["p95_ms"]
    return summary.p95_ms


def summary_max_ms(summary: BenchmarkResult) -> float | None:
    if isinstance(summary, YearsProfileSummary):
        return summary.total["max_ms"]
    if isinstance(summary, ProfileSummary):
        return summary.total["max_ms"]
    return summary.max_ms


def summary_sort_key(summary: BenchmarkResult) -> tuple[int, float, float]:
    p95_ms = summary_p95_ms(summary)
    median_ms = summary_median_ms(summary)
    return (
        summary.threshold_failures,
        p95_ms if p95_ms is not None else float("inf"),
        median_ms if median_ms is not None else float("inf"),
    )


def print_summary(summary: BenchmarkSummary) -> None:
    print("Bildebank browser benchmark")
    print(f"  modus: {summary.mode}")
    print(f"  start: {summary.start_url}")
    print(f"  warmup: {summary.warmup}")
    print(f"  målt: {summary.steps_measured}/{summary.steps_requested}")
    print(
        "  tider: "
        f"median={format_ms(summary.median_ms)}, "
        f"p90={format_ms(summary.p90_ms)}, "
        f"p95={format_ms(summary.p95_ms)}, "
        f"maks={format_ms(summary.max_ms)}, "
        f"snitt={format_ms(summary.mean_ms)}"
    )
    first_byte_values = [step.first_byte_ms for step in summary.steps if step.first_byte_ms is not None]
    read_values = [step.read_ms for step in summary.steps if step.read_ms is not None]
    body_values = [step.body_bytes for step in summary.steps if step.body_bytes is not None]
    server_values = [step.server_ms for step in summary.steps if step.server_ms is not None]
    if first_byte_values or read_values:
        first_byte = stats_dict(first_byte_values)
        read = stats_dict(read_values)
        server = stats_dict(server_values)
        print(
            "  http: "
            f"første_byte_median={format_ms(first_byte['median_ms'])}, "
            f"body_les_median={format_ms(read['median_ms'])}, "
            f"server_median={format_ms(server['median_ms'])}, "
            f"bytes_median={statistics.median(body_values) if body_values else 0:.0f}"
        )
    server_timing = server_timing_medians(summary.steps)
    if server_timing:
        ordered_names = [name for name in SERVER_TIMING_STEP_ORDER if name in server_timing]
        ordered_names.extend(name for name in sorted(server_timing) if name not in ordered_names)
        print("  server: " + ", ".join(f"{name}={format_ms(server_timing[name])}" for name in ordered_names))
    if summary.threshold_ms is not None:
        print(f"  terskel: {summary.threshold_ms:.1f} ms, brudd={summary.threshold_failures}")


def print_profile_summary(summary: ProfileSummary) -> None:
    print("Bildebank browser profile")
    print(f"  start: {summary.start_url}")
    print(f"  target: {summary.target}")
    print(f"  warmup: {summary.warmup}")
    print(f"  målt: {summary.steps_measured}/{summary.steps_requested}")
    print(f"  total: {format_stats(summary.total)}")
    print(f"  db.connect: {format_stats(summary.connect)}")
    print(f"  source_item_by_id: {format_stats(summary.item)}")
    print(f"  adjacent_source_items: {format_stats(summary.adjacent)}")
    print(f"  source_month_navigation: {format_stats(summary.month_nav)}")
    print(f"  source_item_page_html: {format_stats(summary.html)}")
    if summary.threshold_ms is not None:
        print(f"  terskel: {summary.threshold_ms:.1f} ms, brudd={summary.threshold_failures}")


def print_years_profile_summary(summary: YearsProfileSummary) -> None:
    print("Bildebank years profile")
    print(f"  target: {summary.target}")
    print(f"  warmup: {summary.warmup}")
    print(f"  målt: {summary.steps_measured}/{summary.steps_requested}")
    print(f"  år_median: {summary.year_count_median if summary.year_count_median is not None else 0:.0f}")
    print(f"  bytes_median: {summary.html_bytes_median if summary.html_bytes_median is not None else 0:.0f}")
    print(f"  total: {format_stats(summary.total)}")
    print(f"  browser_year_cards: {format_stats(summary.year_cards)}")
    print(f"  browser_year_summaries: {format_stats(summary.year_summaries)}")
    print(f"  thumbnail_items: {format_stats(summary.thumbnail_items)}")
    print(f"  year_card_html: {format_stats(summary.card_html)}")
    print(f"  years_navigation_controls_html: {format_stats(summary.controls)}")
    print(f"  shell_page_html: {format_stats(summary.shell)}")
    if summary.threshold_ms is not None:
        print(f"  terskel: {summary.threshold_ms:.1f} ms, brudd={summary.threshold_failures}")


def print_suite_summary(summary: SuiteSummary, *, show_server_timing: bool = False) -> None:
    print("Bildebank browser benchmark suite")
    print(
        f"  modus: {summary.mode}, repeat={summary.repeat}, "
        f"tillatte brudd={summary.min_failures}–{summary.max_failures}"
    )
    for case in summary.cases:
        best = case.best_run
        failures_per_run = ", ".join(str(run.threshold_failures) for run in case.runs)
        status = "OK" if case.passed else "FEIL"
        print(
            f"  {case.name}: terskel={case.threshold_ms:.1f} ms, "
            f"median={format_ms(summary_median_ms(best))}, "
            f"p95={format_ms(summary_p95_ms(best))}, "
            f"maks={format_ms(summary_max_ms(best))}, "
            f"brudd={best.threshold_failures}, "
            f"brudd/run=[{failures_per_run}] — {status}"
        )
        if show_server_timing and isinstance(best, BenchmarkSummary):
            server_timing = server_timing_medians(best.steps)
            if server_timing:
                ordered_names = [name for name in SERVER_TIMING_STEP_ORDER if name in server_timing]
                ordered_names.extend(name for name in sorted(server_timing) if name not in ordered_names)
                print("    server: " + ", ".join(f"{name}={format_ms(server_timing[name])}" for name in ordered_names))
    print(f"  samlet: {'OK' if summary.passed else 'FEIL'}")


def format_stats(stats: dict[str, float | None]) -> str:
    return (
        f"median={format_ms(stats['median_ms'])}, "
        f"p90={format_ms(stats['p90_ms'])}, "
        f"p95={format_ms(stats['p95_ms'])}, "
        f"maks={format_ms(stats['max_ms'])}, "
        f"snitt={format_ms(stats['mean_ms'])}"
    )


def format_ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f} ms"


def server_timing_medians(steps: list[StepResult]) -> dict[str, float]:
    values_by_name: dict[str, list[float]] = {}
    for step in steps:
        if not step.server_timing_ms:
            continue
        for name, value in step.server_timing_ms.items():
            values_by_name.setdefault(name, []).append(value)
    return {name: statistics.median(values) for name, values in values_by_name.items()}


def summary_to_json(summary: BenchmarkSummary | ProfileSummary | YearsProfileSummary) -> dict[str, Any]:
    data = asdict(summary)
    data["steps"] = [asdict(step) for step in summary.steps]
    return data


def suite_summary_to_json(summary: SuiteSummary) -> dict[str, Any]:
    return {
        "suite_path": summary.suite_path,
        "mode": summary.mode,
        "repeat": summary.repeat,
        "min_failures": summary.min_failures,
        "max_failures": summary.max_failures,
        "passed": summary.passed,
        "cases": [
            {
                "name": case.name,
                "url": case.url,
                "threshold_ms": case.threshold_ms,
                "passed": case.passed,
                "best_run_index": case.best_run_index,
                "best_run": summary_to_json(case.best_run),
                "runs": [summary_to_json(run) for run in case.runs],
            }
            for case in summary.cases
        ],
    }


def write_json_output(path: str | Path, data: dict[str, Any]) -> None:
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
