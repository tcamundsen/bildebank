from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


KERNEL_RELEASE_PATH = Path("/proc/sys/kernel/osrelease")
MOUNTINFO_PATH = Path("/proc/self/mountinfo")


@dataclass(frozen=True)
class MountInfo:
    mount_point: Path
    filesystem_type: str
    mount_source: str
    super_options: tuple[str, ...]


def validate_collection_platform(target: Path) -> None:
    if not is_wsl():
        return
    mount = mount_for_path(target)
    if mount is None or not is_windows_mount(mount):
        return
    raise ValueError(
        "Bildebank kan ikke brukes fra WSL på en bildesamling som ligger på Windows. "
        "Kjør Bildebank direkte i Windows for denne bildesamlingen."
    )


def is_wsl() -> bool:
    if os.name != "posix":
        return False
    try:
        release = KERNEL_RELEASE_PATH.read_text(encoding="utf-8")
    except OSError:
        return False
    return "microsoft" in release.casefold()


def mount_for_path(path: Path) -> MountInfo | None:
    resolved = path.resolve()
    best_match: MountInfo | None = None
    for mount in read_mountinfo():
        try:
            resolved.relative_to(mount.mount_point)
        except ValueError:
            continue
        if (
            best_match is None
            or len(mount.mount_point.parts) > len(best_match.mount_point.parts)
        ):
            best_match = mount
    return best_match


def read_mountinfo() -> tuple[MountInfo, ...]:
    try:
        lines = MOUNTINFO_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    mounts: list[MountInfo] = []
    for line in lines:
        fields = line.split()
        try:
            separator = fields.index("-")
            mount_point = Path(_unescape_mountinfo(fields[4]))
            filesystem_type = fields[separator + 1]
            mount_source = _unescape_mountinfo(fields[separator + 2])
            super_options = tuple(fields[separator + 3].split(","))
        except (IndexError, ValueError):
            continue
        mounts.append(
            MountInfo(
                mount_point=mount_point,
                filesystem_type=filesystem_type,
                mount_source=mount_source,
                super_options=super_options,
            )
        )
    return tuple(mounts)


def is_windows_mount(mount: MountInfo) -> bool:
    if mount.filesystem_type.casefold() == "drvfs":
        return True
    return mount.filesystem_type.casefold() == "9p" and any(
        field.casefold() == "aname=drvfs"
        for option in mount.super_options
        for field in option.split(";")
    )


def _unescape_mountinfo(value: str) -> str:
    for escaped, character in (
        ("\\040", " "),
        ("\\011", "\t"),
        ("\\012", "\n"),
        ("\\134", "\\"),
    ):
        value = value.replace(escaped, character)
    return value
