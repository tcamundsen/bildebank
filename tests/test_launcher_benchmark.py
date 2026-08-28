from __future__ import annotations

import importlib.util
import json
import sys
from argparse import Namespace
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from bildebank.launcher_benchmark import (
    STARTUP_BENCHMARK_FILE_ENV,
    STARTUP_BENCHMARK_START_NS_ENV,
    record_startup_event,
)


def load_benchmark_module() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "tools" / "benchmark_launcher.py"
    spec = importlib.util.spec_from_file_location("benchmark_launcher", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_record_startup_event_writes_elapsed_time_and_pid(tmp_path: Path) -> None:
    event_path = tmp_path / "events.jsonl"
    with (
        patch.dict(
            "os.environ",
            {
                STARTUP_BENCHMARK_FILE_ENV: str(event_path),
                STARTUP_BENCHMARK_START_NS_ENV: "1000000000",
            },
            clear=True,
        ),
        patch("bildebank.launcher_benchmark.time.time_ns", return_value=1_125_000_000),
        patch("bildebank.launcher_benchmark.os.getpid", return_value=123),
    ):
        record_startup_event("test_event")

    assert json.loads(event_path.read_text(encoding="utf-8")) == {
        "name": "test_event",
        "timestamp_ns": 1_125_000_000,
        "elapsed_ms": 125.0,
        "pid": 123,
    }


def test_read_events_ignores_partial_or_invalid_lines(tmp_path: Path) -> None:
    benchmark = load_benchmark_module()
    event_path = tmp_path / "events.jsonl"
    event_path.write_text(
        "\n".join(
            (
                '{"name":"second","timestamp_ns":20,"elapsed_ms":2.0,"pid":2}',
                "partial",
                '{"name":"first","timestamp_ns":10,"elapsed_ms":1.0,"pid":1}',
            )
        ),
        encoding="utf-8",
    )

    events = benchmark.read_events(event_path)

    assert [event.name for event in events] == ["first", "second"]


def test_parse_args_accepts_explicit_launcher_command() -> None:
    benchmark = load_benchmark_module()

    args = benchmark.parse_args(["--repeat", "2", "--", "bildebank", "start"])

    assert args.repeat == 2
    assert args.command == ["bildebank", "start"]


def test_main_writes_json_summary(tmp_path: Path) -> None:
    benchmark = load_benchmark_module()
    output_path = tmp_path / "result.json"
    events = [
        benchmark.StartupEvent("benchmark_start", 10, 0.0, 1),
        benchmark.StartupEvent("window_visible", 20, 250.0, 2),
    ]
    run = benchmark.StartupRun(index=1, total_ms=250.0, events=events)
    args = Namespace(
        command=["bildebank", "start"],
        repeat=1,
        timeout=30.0,
        json_output=output_path,
    )

    with (
        patch.object(benchmark, "parse_args", return_value=args),
        patch.object(benchmark, "run_launcher_startup", return_value=run) as run_startup,
        patch.object(benchmark, "print_run"),
    ):
        assert benchmark.main([]) == 0

    run_startup.assert_called_once_with(
        ["bildebank", "start"],
        index=1,
        timeout_seconds=30.0,
    )
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["median_ms"] == 250.0
    assert result["runs"][0]["events"][1]["name"] == "window_visible"
