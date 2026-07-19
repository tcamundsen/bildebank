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


@dataclass(frozen=True)
class SnapshotPlanProgress:
    stage: str
    completed_objects: int = 0
    total_objects: int = 0
    completed_bytes: int = 0
    total_bytes: int = 0


SnapshotPlanProgressCallback = Callable[[SnapshotPlanProgress], None]

SnapshotCancelCallback = Callable[[], bool]


class SnapshotCancelled(RuntimeError):
    pass


def raise_if_snapshot_cancelled(should_cancel: SnapshotCancelCallback | None) -> None:
    if should_cancel is not None and should_cancel():
        raise SnapshotCancelled("Snapshot-operasjonen ble avbrutt kontrollert.")
