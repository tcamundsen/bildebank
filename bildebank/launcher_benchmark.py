from __future__ import annotations

import json
import os
import time
from pathlib import Path


STARTUP_BENCHMARK_FILE_ENV = "BILDEBANK_LAUNCHER_BENCHMARK_FILE"
STARTUP_BENCHMARK_START_NS_ENV = "BILDEBANK_LAUNCHER_BENCHMARK_START_NS"
STARTUP_BENCHMARK_AUTO_CLOSE_ENV = "BILDEBANK_LAUNCHER_BENCHMARK_AUTO_CLOSE"


def startup_benchmark_enabled() -> bool:
    return bool(os.environ.get(STARTUP_BENCHMARK_FILE_ENV))


def startup_benchmark_auto_close() -> bool:
    return os.environ.get(STARTUP_BENCHMARK_AUTO_CLOSE_ENV) == "1"


def record_startup_event(name: str) -> None:
    """Append one opt-in launcher startup event without affecting normal startup."""
    raw_path = os.environ.get(STARTUP_BENCHMARK_FILE_ENV)
    raw_start_ns = os.environ.get(STARTUP_BENCHMARK_START_NS_ENV)
    if not raw_path or not raw_start_ns:
        return
    try:
        start_ns = int(raw_start_ns)
        timestamp_ns = time.time_ns()
        payload = {
            "name": name,
            "timestamp_ns": timestamp_ns,
            "elapsed_ms": (timestamp_ns - start_ns) / 1_000_000,
            "pid": os.getpid(),
        }
        with Path(raw_path).open("a", encoding="utf-8") as event_file:
            event_file.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        # Benchmarking must never prevent the launcher from opening.
        return
