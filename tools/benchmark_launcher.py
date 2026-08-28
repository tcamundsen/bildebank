#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bildebank.launcher_benchmark import (  # noqa: E402
    STARTUP_BENCHMARK_AUTO_CLOSE_ENV,
    STARTUP_BENCHMARK_FILE_ENV,
    STARTUP_BENCHMARK_START_NS_ENV,
)


DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class StartupEvent:
    name: str
    timestamp_ns: int
    elapsed_ms: float
    pid: int


@dataclass(frozen=True)
class StartupRun:
    index: int
    total_ms: float
    events: list[StartupEvent]


@dataclass(frozen=True)
class StartupSummary:
    command: list[str]
    repeat: int
    median_ms: float
    min_ms: float
    max_ms: float
    runs: list[StartupRun]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    command = args.command or [sys.executable, "-m", "bildebank", "start"]
    runs: list[StartupRun] = []
    try:
        for index in range(1, args.repeat + 1):
            run = run_launcher_startup(command, index=index, timeout_seconds=args.timeout)
            runs.append(run)
            print_run(run)
    except (OSError, RuntimeError) as exc:
        print(f"FEIL: {exc}", file=sys.stderr)
        return 2

    totals = [run.total_ms for run in runs]
    summary = StartupSummary(
        command=command,
        repeat=args.repeat,
        median_ms=statistics.median(totals),
        min_ms=min(totals),
        max_ms=max(totals),
        runs=runs,
    )
    if len(runs) > 1:
        print(
            f"Samlet: median={summary.median_ms:.1f} ms "
            f"min={summary.min_ms:.1f} ms maks={summary.max_ms:.1f} ms"
        )
    if args.json_output is not None:
        try:
            args.json_output.write_text(
                json.dumps(asdict(summary), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"FEIL: Kunne ikke skrive JSON-resultatet: {exc}", file=sys.stderr)
            return 2
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mål tiden fra launcherprosessen startes til Bildebank-vinduet er synlig."
    )
    parser.add_argument("--repeat", type=positive_int, default=1, help="Antall kjøringer. Standard: 1")
    parser.add_argument(
        "--timeout",
        type=positive_float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Tidsgrense per kjøring i sekunder. Standard: {DEFAULT_TIMEOUT_SECONDS:g}",
    )
    parser.add_argument("--json-output", type=Path, help="Skriv komplett resultat som JSON.")
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Valgfri launcherkommando etter --. Standard er gjeldende Python med -m bildebank start.",
    )
    args = parser.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if args.command == []:
        args.command = None
    return args


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("må være minst 1")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("må være større enn 0")
    return parsed


def run_launcher_startup(command: list[str], *, index: int, timeout_seconds: float) -> StartupRun:
    with tempfile.TemporaryDirectory(prefix="bildebank-launcher-benchmark-") as temp_dir:
        event_path = Path(temp_dir) / "events.jsonl"
        start_ns = time.time_ns()
        write_initial_event(event_path, start_ns)
        environment = os.environ.copy()
        environment[STARTUP_BENCHMARK_FILE_ENV] = str(event_path)
        environment[STARTUP_BENCHMARK_START_NS_ENV] = str(start_ns)
        environment[STARTUP_BENCHMARK_AUTO_CLOSE_ENV] = "1"
        process = subprocess.Popen(command, env=environment)
        deadline = time.monotonic() + timeout_seconds
        events: list[StartupEvent] = []
        while time.monotonic() < deadline:
            events = read_events(event_path)
            if any(event.name == "window_visible" for event in events):
                break
            return_code = process.poll()
            if return_code not in (None, 0):
                raise RuntimeError(f"Launcherkommandoen avsluttet med status {return_code} før vinduet ble synlig.")
            time.sleep(0.02)
        else:
            raise RuntimeError(f"Vinduet ble ikke synlig innen {timeout_seconds:g} sekunder.")

        visible_event = next(event for event in events if event.name == "window_visible")
        wait_for_exit_event(event_path, time.monotonic() + 2.0)
        return StartupRun(index=index, total_ms=visible_event.elapsed_ms, events=read_events(event_path))


def write_initial_event(path: Path, start_ns: int) -> None:
    payload = {
        "name": "benchmark_start",
        "timestamp_ns": start_ns,
        "elapsed_ms": 0.0,
        "pid": os.getpid(),
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def read_events(path: Path) -> list[StartupEvent]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    events: list[StartupEvent] = []
    for line in lines:
        try:
            raw: dict[str, Any] = json.loads(line)
            events.append(
                StartupEvent(
                    name=str(raw["name"]),
                    timestamp_ns=int(raw["timestamp_ns"]),
                    elapsed_ms=float(raw["elapsed_ms"]),
                    pid=int(raw["pid"]),
                )
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return sorted(events, key=lambda event: event.timestamp_ns)


def wait_for_exit_event(path: Path, deadline: float) -> None:
    while time.monotonic() < deadline:
        if any(event.name == "mainloop_exit" for event in read_events(path)):
            return
        time.sleep(0.02)


def print_run(run: StartupRun) -> None:
    print(f"Kjøring {run.index}: vindu synlig etter {run.total_ms:.1f} ms")
    previous_ms = 0.0
    for event in run.events:
        phase_ms = event.elapsed_ms - previous_ms
        print(f"  {event.elapsed_ms:8.1f} ms  +{phase_ms:8.1f} ms  {event.name} (pid {event.pid})")
        previous_ms = event.elapsed_ms


if __name__ == "__main__":
    raise SystemExit(main())
