from __future__ import annotations

import datetime as dt
import os
import socket
from pathlib import Path


LOCK_FILENAME = ".bildebank.lock"


class TargetLock:
    def __init__(self, target: Path, *, command: str) -> None:
        self.path = target / LOCK_FILENAME
        self.command = command
        self.fd: int | None = None

    def __enter__(self) -> TargetLock:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            self.fd = os.open(self.path, flags)
        except FileExistsError as exc:
            details = lock_details(self.path)
            message = (
                "Målmappen er låst av en annen bildebank-kommando. "
                "Vent til den er ferdig før du kjører import på nytt."
            )
            if details:
                message = f"{message}\n{details}"
            raise RuntimeError(message) from exc

        created = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        content = (
            f"command={self.command}\n"
            f"pid={os.getpid()}\n"
            f"host={socket.gethostname()}\n"
            f"created_at={created}\n"
        )
        os.write(self.fd, content.encode("utf-8"))
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def lock_details(path: Path) -> str:
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        return f"Lockfil: {path}"
    if not content:
        return f"Lockfil: {path}"
    return f"Lockfil: {path}\n{content}"
