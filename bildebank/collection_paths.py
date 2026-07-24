from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath


class InvalidCollectionRelativePath(ValueError):
    pass


class CollectionFileHashError(ValueError):
    pass


COLLECTION_FILE_MISSING = "missing"
COLLECTION_FILE_NOT_REGULAR = "not_regular"
COLLECTION_FILE_OK = "ok"
COLLECTION_FILE_UNSAFE = "unsafe"
COLLECTION_FILE_UNREADABLE = "unreadable"


@dataclass(frozen=True)
class PathComponentIssue:
    path: Path
    reason: str


@dataclass(frozen=True)
class CollectionFileInspection:
    path: Path
    status: str
    size_bytes: int | None = None
    message: str | None = None
    path_stat: os.stat_result | None = None


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


def inspect_collection_file(
    target: Path,
    relative_path: Path,
) -> CollectionFileInspection:
    relative_path = parse_collection_relative_path(relative_path.as_posix())
    file_path = target / relative_path

    parent_parts = relative_path.parts[:-1]
    if parent_parts:
        component_issue = inspect_existing_collection_path_components(
            target,
            Path(*parent_parts),
        )
        if component_issue is not None:
            return CollectionFileInspection(
                path=file_path,
                status=COLLECTION_FILE_UNSAFE,
                message=f"{component_issue.path}: {component_issue.reason}",
            )

    try:
        path_stat = file_path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return CollectionFileInspection(
            path=file_path,
            status=COLLECTION_FILE_MISSING,
        )
    except OSError as exc:
        return CollectionFileInspection(
            path=file_path,
            status=COLLECTION_FILE_UNREADABLE,
            message=str(exc),
        )

    if (
        stat.S_ISLNK(path_stat.st_mode)
        or is_reparse_stat(path_stat)
        or not stat.S_ISREG(path_stat.st_mode)
    ):
        return CollectionFileInspection(
            path=file_path,
            status=COLLECTION_FILE_NOT_REGULAR,
            message="er ikke en vanlig fil uten lenker",
        )
    return CollectionFileInspection(
        path=file_path,
        status=COLLECTION_FILE_OK,
        size_bytes=path_stat.st_size,
        path_stat=path_stat,
    )


def hash_stable_collection_file(
    target: Path,
    relative_path: Path,
    *,
    chunk_size: int = 1024 * 1024,
) -> tuple[str, int]:
    if chunk_size <= 0:
        raise ValueError("chunk_size må være større enn 0")
    before = inspect_collection_file(target, relative_path)
    if before.status != COLLECTION_FILE_OK or before.path_stat is None:
        raise CollectionFileHashError(
            before.message or "filen finnes ikke som en vanlig fil uten lenker"
        )

    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        file_descriptor = os.open(before.path, flags)
    except OSError as exc:
        raise CollectionFileHashError(
            f"filen kunne ikke åpnes uten å følge lenker: {exc}"
        ) from exc

    digest = hashlib.sha256()
    size_bytes = 0
    try:
        opened_before = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(opened_before.st_mode)
            or is_reparse_stat(opened_before)
            or not same_file_identity(before.path_stat, opened_before)
            or before.path_stat.st_size != opened_before.st_size
            or before.path_stat.st_mtime_ns != opened_before.st_mtime_ns
        ):
            raise CollectionFileHashError(
                "filen ble byttet eller endret før hashing"
            )

        with os.fdopen(file_descriptor, "rb", closefd=True) as stream:
            file_descriptor = -1
            while True:
                chunk = stream.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
                size_bytes += len(chunk)
            opened_after = os.fstat(stream.fileno())
    except BaseException:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        raise

    after = inspect_collection_file(target, relative_path)
    if after.status != COLLECTION_FILE_OK or after.path_stat is None:
        raise CollectionFileHashError(
            "filen forsvant eller ble utrygg under hashing"
        )
    if not (
        same_file_identity(before.path_stat, opened_after)
        and same_file_identity(opened_after, after.path_stat)
        and before.path_stat.st_size
        == opened_after.st_size
        == after.path_stat.st_size
        == size_bytes
        and before.path_stat.st_mtime_ns
        == opened_after.st_mtime_ns
        == after.path_stat.st_mtime_ns
    ):
        raise CollectionFileHashError("filen ble endret under hashing")
    return digest.hexdigest(), size_bytes


def same_file_identity(
    first: os.stat_result,
    second: os.stat_result,
) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def is_reparse_stat(path_stat: os.stat_result) -> bool:
    attributes = getattr(path_stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)
