from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath


class InvalidCollectionRelativePath(ValueError):
    pass


@dataclass(frozen=True)
class PathComponentIssue:
    path: Path
    reason: str


def parse_collection_relative_path(value: object) -> Path:
    if not isinstance(value, str):
        raise InvalidCollectionRelativePath("må være tekst")
    if not value:
        raise InvalidCollectionRelativePath("kan ikke være tom")
    if "\x00" in value:
        raise InvalidCollectionRelativePath("kan ikke inneholde NUL-tegn")

    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if posix_path.is_absolute() or windows_path.drive or windows_path.root:
        raise InvalidCollectionRelativePath("må være relativ til bildesamlingen")
    if "\\" in value:
        raise InvalidCollectionRelativePath(
            "må bruke / som skilletegn, ikke backslash"
        )

    parts = value.split("/")
    if any(not part for part in parts):
        raise InvalidCollectionRelativePath("kan ikke ha tomme stikomponenter")
    if any(part == "." for part in parts):
        raise InvalidCollectionRelativePath("kan ikke inneholde .")
    if any(part == ".." for part in parts):
        raise InvalidCollectionRelativePath("kan ikke inneholde ..")
    return Path(*parts)


def is_active_collection_file_path(path: Path) -> bool:
    parts = path.parts
    if len(parts) == 2 and parts[0] == "udatert":
        return True
    if len(parts) != 3:
        return False
    year, month, _filename = parts
    return (
        len(year) == 4
        and year.isascii()
        and year.isdigit()
        and year != "0000"
        and len(month) == 2
        and month.isascii()
        and month.isdigit()
        and 1 <= int(month) <= 12
    )


def is_deleted_collection_file_path(path: Path) -> bool:
    return (
        len(path.parts) >= 2
        and path.parts[0] == "deleted"
        and is_active_collection_file_path(Path(*path.parts[1:]))
    )


def inspect_existing_collection_path_components(
    target: Path,
    relative_path: Path,
) -> PathComponentIssue | None:
    relative_path = parse_collection_relative_path(relative_path.as_posix())
    current = target
    for part in relative_path.parts:
        current = current / part
        try:
            path_stat = current.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            return PathComponentIssue(
                current,
                f"stikomponenten kunne ikke kontrolleres: {exc}",
            )
        if stat.S_ISLNK(path_stat.st_mode) or is_reparse_stat(path_stat):
            return PathComponentIssue(
                current,
                "stikomponenten er en symlink eller et Windows reparse point",
            )
    return None


def is_reparse_stat(path_stat: os.stat_result) -> bool:
    attributes = getattr(path_stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)
