from __future__ import annotations

import sys
import time
from typing import TextIO


class ProgressLine:
    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout
        self.interactive = self.stream.isatty()
        self.active = False

    def write(self, message: str) -> None:
        if self.interactive:
            print(f"\r{message}\033[K", end="", file=self.stream, flush=True)
            self.active = True
        else:
            print(message, file=self.stream, flush=True)

    def finish(self) -> None:
        if self.interactive and self.active:
            print(file=self.stream, flush=True)
            self.active = False


class ProgressMeter:
    def __init__(
        self,
        label: str,
        *,
        stream: TextIO | None = None,
        item_interval: int = 25,
        time_interval_seconds: float = 0.5,
    ) -> None:
        self.label = label
        self.line = ProgressLine(stream or sys.stdout)
        self.item_interval = item_interval
        self.time_interval_seconds = time_interval_seconds
        self.started_at: float | None = None
        self.last_update_at = 0.0

    def reset_eta(self) -> None:
        self.started_at = None
        self.last_update_at = 0.0

    def message(self, message: str) -> None:
        self.line.write(message)

    def update(
        self,
        current: int,
        total: int,
        *,
        action: str,
        details: str = "",
        eta: bool = False,
        force: bool = False,
    ) -> None:
        if not self.should_update(current, total, force=force):
            return
        parts = [f"{self.label}: {action}={min(current, total)}/{total}"]
        if details:
            parts.append(details)
        if eta:
            parts.append(f"gjenstår={self.eta_text(current, total)}")
        self.line.write(", ".join(parts))

    def update_count(
        self,
        current: int,
        *,
        action: str,
        details: str = "",
        force: bool = False,
    ) -> None:
        if not self.should_update_count(current, force=force):
            return
        parts = [f"{self.label}: {action}={current}"]
        if details:
            parts.append(details)
        self.line.write(", ".join(parts))

    def error(self, message: str) -> None:
        self.line.finish()
        print(message, file=self.line.stream, flush=True)

    def done(self, message: str | None = None) -> None:
        if message is not None:
            self.line.write(message)
        self.line.finish()

    def should_update(self, current: int, total: int, *, force: bool = False) -> bool:
        if force or total <= 20 or current >= total:
            return True
        if self.item_interval > 0 and current % self.item_interval == 0:
            return True
        now = time.monotonic()
        if now - self.last_update_at >= self.time_interval_seconds:
            self.last_update_at = now
            return True
        return False

    def should_update_count(self, current: int, *, force: bool = False) -> bool:
        if force:
            return True
        if self.item_interval > 0 and current % self.item_interval == 0:
            return True
        now = time.monotonic()
        if now - self.last_update_at >= self.time_interval_seconds:
            self.last_update_at = now
            return True
        return False

    def eta_text(self, current: int, total: int) -> str:
        if current <= 0:
            return "ukjent"
        if self.started_at is None:
            self.started_at = time.monotonic()
        remaining = total - current
        if remaining <= 0:
            return "0s"
        elapsed = max(time.monotonic() - self.started_at, 0.0)
        if current < 3 and elapsed < 5.0:
            return "beregner"
        return format_duration(elapsed * remaining / current)


def format_duration(seconds: float) -> str:
    seconds = max(int(round(seconds)), 0)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}t {minutes:02d}m"
