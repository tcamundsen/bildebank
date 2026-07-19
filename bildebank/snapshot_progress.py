from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class SnapshotCreateProgress:
    stage: str
    completed_objects: int = 0
    total_objects: int = 0
    completed_bytes: int = 0
    total_bytes: int = 0
    current_path: str | None = None


SnapshotCreateProgressCallback = Callable[[SnapshotCreateProgress], None]
