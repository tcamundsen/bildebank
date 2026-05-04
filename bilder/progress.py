from __future__ import annotations

import sys
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
