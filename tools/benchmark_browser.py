#!/usr/bin/env python3
from __future__ import annotations

import argparse
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
        if args.mode == "browser":
            summary = run_browser_benchmark(args)
        else:
            summary = run_server_benchmark(args)
    except RuntimeError as exc:
        print(f"FEIL: {exc}", file=sys.stderr)
        return 2

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
    parser.add_argument("--mode", choices=("browser", "server"), default="browser")
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
    for _ in range(args.warmup):
        _elapsed_ms, url = fetch_next(url, args.timeout_ms)
    steps: list[StepResult] = []
    for index in range(1, args.steps + 1):
        try:
            elapsed_ms, url = fetch_next(url, args.timeout_ms)
        except RuntimeError:
            break
        steps.append(StepResult(index=index, elapsed_ms=elapsed_ms, url=url))
    return build_summary("server", args, steps)


def fetch_next(url: str, timeout_ms: int) -> tuple[float, str]:
    start = time.perf_counter()
    html = fetch_text(url, timeout_ms)
    parser = NextLinkParser()
    parser.feed(html)
    if parser.next_href is None:
        raise RuntimeError("Fant ingen aktiv Neste bilde-lenke.")
    next_url = urllib.parse.urljoin(url, parser.next_href)
    html = fetch_text(next_url, timeout_ms)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    if not html:
        raise RuntimeError(f"Tom respons fra {next_url}")
    return elapsed_ms, next_url


def fetch_text(url: str, timeout_ms: int) -> str:
    with urllib.request.urlopen(url, timeout=timeout_ms / 1000.0) as response:
        encoding = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(encoding, errors="replace")


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
    if summary.threshold_ms is not None:
        print(f"  terskel: {summary.threshold_ms:.1f} ms, brudd={summary.threshold_failures}")


def format_ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f} ms"


def summary_to_json(summary: BenchmarkSummary) -> dict[str, Any]:
    data = asdict(summary)
    data["steps"] = [asdict(step) for step in summary.steps]
    return data


if __name__ == "__main__":
    raise SystemExit(main())
