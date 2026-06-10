#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.client
import json
import statistics
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


DEFAULT_URL = "http://127.0.0.1:8765/"


@dataclass(frozen=True)
class StepResult:
    index: int
    elapsed_ms: float
    url: str
    first_byte_ms: float | None = None
    read_ms: float | None = None
    body_bytes: int | None = None


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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.mode == "profile":
            summary = run_profile_benchmark(args)
        elif args.mode == "browser":
            summary = run_browser_benchmark(args)
        elif args.mode == "server-keepalive":
            summary = run_server_keepalive_benchmark(args)
        else:
            summary = run_server_benchmark(args)
    except RuntimeError as exc:
        print(f"FEIL: {exc}", file=sys.stderr)
        return 2

    if isinstance(summary, ProfileSummary):
        print_profile_summary(summary)
    else:
        print_summary(summary)
    if args.json_output:
        Path(args.json_output).write_text(
            json.dumps(summary_to_json(summary), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
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
    parser.add_argument("--mode", choices=("browser", "server", "server-keepalive", "profile"), default="browser")
    parser.add_argument("--target", type=Path, help="Bildesamlingsmappe. Kreves for --mode profile.")
    parser.add_argument("--steps", type=positive_int, default=50, help="Antall neste-klikk som måles. Standard: 50")
    parser.add_argument("--warmup", type=non_negative_int, default=5, help="Antall oppvarmingsklikk før måling. Standard: 5")
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
        html, _first_byte_ms, _read_ms, _body_bytes = fetch_text_keepalive_timed(conn, url)
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


def run_profile_benchmark(args: argparse.Namespace) -> ProfileSummary:
    from bildebank import db

    if args.target is None:
        raise RuntimeError("--target må oppgis med --mode profile.")
    target = args.target
    if not target.exists() or not target.is_dir():
        raise RuntimeError(f"Bildesamlingen finnes ikke som mappe: {target}")
    db.prepare_database(target)

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
        )
        steps.append(step)
        next_file_id = next_profile_file_id(step.url)
        if next_file_id is None:
            break
        current_file_id = next_file_id
    return build_profile_summary(args, steps)


def profile_source_and_file_id(target: Path, url: str) -> tuple[Any, int]:
    from bildebank.server_browser_sources import all_browser_source, parse_source_path
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
    raise RuntimeError("Profile-modus støtter foreløpig /item/<id> og /filter/<query>/item/<id>.")


def profile_cached_month_keys(target: Path, source: Any) -> list[str] | None:
    from bildebank.server_browser import browser_month_keys
    from bildebank.server_browser_sources import all_browser_source

    if source != all_browser_source():
        return None
    return browser_month_keys(target)


def profile_cached_item_ids(target: Path, source: Any) -> list[int] | None:
    from bildebank.server_browser import browser_item_ids
    from bildebank.server_browser_sources import all_browser_source

    if source != all_browser_source():
        return None
    return browser_item_ids(target)


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
) -> ProfileStepResult:
    from bildebank import db
    from bildebank.server_browser import (
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
        html = source_item_page_html(target, source, item, previous_item, next_item, month_nav, conn=conn)
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


def fetch_next_page_keepalive(conn: http.client.HTTPConnection, url: str, html: str) -> tuple[float, str, str]:
    parser = NextLinkParser()
    parser.feed(html)
    if parser.next_href is None:
        raise RuntimeError("Fant ingen aktiv Neste bilde-lenke.")
    next_url = urllib.parse.urljoin(url, parser.next_href)
    start = time.perf_counter()
    next_html = fetch_text_keepalive(conn, next_url)
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
    next_html, first_byte_ms, read_ms, body_bytes = fetch_text_keepalive_timed(conn, next_url)
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


def fetch_text_keepalive_timed(conn: http.client.HTTPConnection, url: str) -> tuple[str, float, float, int]:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"
    start = time.perf_counter()
    conn.request("GET", path)
    response = conn.getresponse()
    first_byte_ms = elapsed_ms(start)
    try:
        if response.status >= 400:
            raise RuntimeError(f"HTTP {response.status} fra {url}")
        content_type = response.headers.get("Content-Type", "")
        encoding = "utf-8"
        if "charset=" in content_type:
            encoding = content_type.rsplit("charset=", 1)[1].split(";", 1)[0].strip() or encoding
        start = time.perf_counter()
        content = response.read()
        read_ms = elapsed_ms(start)
        return content.decode(encoding, errors="replace"), first_byte_ms, read_ms, len(content)
    finally:
        response.close()


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
    if first_byte_values or read_values:
        first_byte = stats_dict(first_byte_values)
        read = stats_dict(read_values)
        print(
            "  http: "
            f"første_byte_median={format_ms(first_byte['median_ms'])}, "
            f"body_les_median={format_ms(read['median_ms'])}, "
            f"bytes_median={statistics.median(body_values) if body_values else 0:.0f}"
        )
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


def summary_to_json(summary: BenchmarkSummary | ProfileSummary) -> dict[str, Any]:
    data = asdict(summary)
    data["steps"] = [asdict(step) for step in summary.steps]
    return data


if __name__ == "__main__":
    raise SystemExit(main())
