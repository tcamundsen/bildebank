from __future__ import annotations

import os
import stat
from pathlib import Path

from .collection_paths import is_reparse_stat


class UnsafeSidecarPath(ValueError):
    pass


def validate_existing_path_components_without_links(path: Path) -> None:
    absolute = path.expanduser().absolute()
    components = [absolute, *absolute.parents]
    for component in reversed(components):
        try:
            component_stat = component.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise UnsafeSidecarPath(
                f"Kunne ikke kontrollere stikomponent: {component}: {exc}"
            ) from exc
        if stat.S_ISLNK(component_stat.st_mode) or is_reparse_stat(component_stat):
            raise UnsafeSidecarPath(
                "Sidecar-stien kan ikke gå gjennom en symlink eller et "
                f"Windows reparse point: {component}"
            )


def validate_regular_database_file(path: Path) -> os.stat_result:
    validate_existing_path_components_without_links(path)
    try:
        path_stat = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        raise UnsafeSidecarPath(f"Sidecar-databasen finnes ikke: {path}") from None
    except OSError as exc:
        raise UnsafeSidecarPath(
            f"Sidecar-databasen kunne ikke kontrolleres: {path}: {exc}"
        ) from exc
    if (
        stat.S_ISLNK(path_stat.st_mode)
        or is_reparse_stat(path_stat)
        or not stat.S_ISREG(path_stat.st_mode)
    ):
        raise UnsafeSidecarPath(
            f"Sidecar-databasen er ikke en vanlig fil uten lenker: {path}"
        )
    if getattr(path_stat, "st_nlink", 1) != 1:
        raise UnsafeSidecarPath(
            f"Sidecar-databasen kan ikke være en hardlink: {path}"
        )
    return path_stat


def regular_database_file_exists(path: Path) -> bool:
    validate_existing_path_components_without_links(path)
    try:
        path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise UnsafeSidecarPath(
            f"Sidecar-databasen kunne ikke kontrolleres: {path}: {exc}"
        ) from exc
    validate_regular_database_file(path)
    return True


def regular_directory_exists_without_links(path: Path) -> bool:
    validate_existing_path_components_without_links(path)
    try:
        path_stat = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise UnsafeSidecarPath(
            f"Sidecar-mappen kunne ikke kontrolleres: {path}: {exc}"
        ) from exc
    if (
        stat.S_ISLNK(path_stat.st_mode)
        or is_reparse_stat(path_stat)
        or not stat.S_ISDIR(path_stat.st_mode)
    ):
        raise UnsafeSidecarPath(
            f"Sidecar-mappen er ikke en vanlig mappe uten lenker: {path}"
        )
    return True


def ensure_directory_without_links(path: Path) -> Path:
    validate_existing_path_components_without_links(path)
    try:
        path.mkdir()
    except FileExistsError:
        pass
    except OSError as exc:
        raise UnsafeSidecarPath(
            f"Sidecar-mappen kunne ikke opprettes: {path}: {exc}"
        ) from exc
    if not regular_directory_exists_without_links(path):
        raise UnsafeSidecarPath(f"Sidecar-mappen kunne ikke opprettes: {path}")
    return path


def create_or_validate_database_file(path: Path) -> Path:
    try:
        return create_new_database_file(path)
    except FileExistsError:
        validate_regular_database_file(path)
        return path


def create_new_database_file(path: Path) -> Path:
    validate_existing_path_components_without_links(path.parent)
    try:
        parent_stat = path.parent.stat(follow_symlinks=False)
    except OSError as exc:
        raise UnsafeSidecarPath(
            f"Sidecar-databasens mappe kunne ikke kontrolleres: {path.parent}: {exc}"
        ) from exc
    if (
        stat.S_ISLNK(parent_stat.st_mode)
        or is_reparse_stat(parent_stat)
        or not stat.S_ISDIR(parent_stat.st_mode)
    ):
        raise UnsafeSidecarPath(
            "Sidecar-databasens mappe er ikke en vanlig mappe uten lenker: "
            f"{path.parent}"
        )

    flags = (
        os.O_CREAT
        | os.O_EXCL
        | os.O_RDWR
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_descriptor = -1
    try:
        file_descriptor = os.open(path, flags, 0o600)
        created_stat = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(created_stat.st_mode)
            or is_reparse_stat(created_stat)
            or getattr(created_stat, "st_nlink", 1) != 1
        ):
            raise UnsafeSidecarPath(
                f"Kunne ikke opprette en trygg sidecar-databasefil: {path}"
            )
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)

    validate_regular_database_file(path)
    return path


def sqlite_read_write_uri(path: Path) -> str:
    return f"{path.absolute().as_uri()}?mode=rw"
