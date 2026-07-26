from __future__ import annotations

import ntpath
import os
import stat
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .collection_paths import is_reparse_stat


DOWNLOAD_CHUNK_BYTES = 1024 * 1024
WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)


def download_https_file(
    url: str,
    destination: Path,
    *,
    user_agent: str,
    max_bytes: int,
    expected_size: int | None = None,
    timeout_seconds: float = 60,
) -> int:
    if urlsplit(url).scheme.lower() != "https":
        raise RuntimeError(f"Nedlastingsadressen må bruke HTTPS: {url}")
    if max_bytes <= 0:
        raise ValueError("max_bytes må være større enn null")
    if expected_size is not None and not 0 <= expected_size <= max_bytes:
        raise ValueError("expected_size må være mellom null og max_bytes")

    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    flags = (
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_descriptor = -1
    destination_created = False
    try:
        file_descriptor = os.open(destination, flags, 0o600)
        destination_created = True
        opened_stat = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or is_reparse_stat(opened_stat)
            or getattr(opened_stat, "st_nlink", 1) != 1
        ):
            raise RuntimeError(
                f"Kunne ikke opprette en trygg nedlastingsfil: {destination}"
            )

        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            final_url = response.geturl()
            if urlsplit(final_url).scheme.lower() != "https":
                raise RuntimeError(
                    f"Nedlastingen ble omdirigert til en adresse uten HTTPS: {final_url}"
                )
            content_length = _response_content_length(response)
            if content_length is not None and content_length > max_bytes:
                raise RuntimeError(
                    f"Nedlastingen er større enn tillatt grense på {max_bytes} byte."
                )

            downloaded = 0
            with os.fdopen(file_descriptor, "wb", closefd=True) as output:
                file_descriptor = -1
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        raise RuntimeError(
                            "Nedlastingen overskred tillatt grense på "
                            f"{max_bytes} byte."
                        )
                    output.write(chunk)

        if expected_size is not None and downloaded != expected_size:
            raise RuntimeError(
                "Nedlastingen har feil størrelse: "
                f"forventet {expected_size}, fikk {downloaded}."
            )
        return downloaded
    except BaseException:
        if file_descriptor >= 0:
            try:
                os.close(file_descriptor)
            finally:
                file_descriptor = -1
        if destination_created:
            destination.unlink(missing_ok=True)
        raise
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)


def ensure_directory_without_links(path: Path, *, label: str) -> Path:
    absolute = path.expanduser().absolute()
    missing: list[Path] = []
    for current in (absolute, *absolute.parents):
        try:
            current_stat = current.stat(follow_symlinks=False)
        except FileNotFoundError:
            missing.append(current)
            continue
        except OSError as exc:
            raise RuntimeError(f"Kunne ikke kontrollere {label}: {current}: {exc}") from exc
        _require_regular_directory(current, current_stat, label=label)

    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            pass
        except OSError as exc:
            raise RuntimeError(f"Kunne ikke opprette {label}: {directory}: {exc}") from exc
        try:
            directory_stat = directory.stat(follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError(
                f"Kunne ikke kontrollere opprettet {label}: {directory}: {exc}"
            ) from exc
        _require_regular_directory(directory, directory_stat, label=label)
    return absolute


def reject_directory_link(path: Path, *, label: str) -> None:
    try:
        path_stat = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError(f"Kunne ikke kontrollere {label}: {path}: {exc}") from exc
    _require_regular_directory(path, path_stat, label=label)


def validate_regular_file_without_links(
    path: Path,
    *,
    label: str,
    expected_size: int | None = None,
) -> os.stat_result:
    absolute = path.expanduser().absolute()
    components = [absolute, *absolute.parents]
    file_stat: os.stat_result | None = None
    for index, component in enumerate(components):
        try:
            component_stat = component.stat(follow_symlinks=False)
        except FileNotFoundError:
            raise FileNotFoundError(f"Fant ikke {label}: {path}") from None
        except OSError as exc:
            raise RuntimeError(f"Kunne ikke kontrollere {label}: {component}: {exc}") from exc
        if index == 0:
            if (
                stat.S_ISLNK(component_stat.st_mode)
                or is_reparse_stat(component_stat)
                or not stat.S_ISREG(component_stat.st_mode)
                or getattr(component_stat, "st_nlink", 1) != 1
            ):
                raise RuntimeError(
                    f"{label} må være en vanlig fil uten lenker: {path}"
                )
            file_stat = component_stat
        else:
            _require_regular_directory(component, component_stat, label=label)

    if file_stat is None:
        raise RuntimeError(f"Kunne ikke kontrollere {label}: {path}")
    if expected_size is not None and file_stat.st_size != expected_size:
        raise RuntimeError(
            f"{label} har feil størrelse: forventet {expected_size}, "
            f"fikk {file_stat.st_size}: {path}"
        )
    return file_stat


def safe_extract_zip(
    archive_path: Path,
    destination: Path,
    *,
    label: str,
    max_members: int,
    max_uncompressed_bytes: int,
) -> None:
    destination = ensure_directory_without_links(
        destination,
        label=f"utpakkingsmappe for {label}",
    )
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) > max_members:
            raise RuntimeError(
                f"{label}-arkivet har for mange filer: {len(members)}."
            )

        total_size = 0
        seen: set[str] = set()
        validated: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
        for member in members:
            relative = _validated_zip_member_path(member, label=label)
            collision_key = relative.as_posix().casefold()
            if collision_key in seen:
                raise RuntimeError(
                    f"{label}-arkivet har duplisert filsti: {member.filename}"
                )
            seen.add(collision_key)
            if not member.is_dir():
                total_size += member.file_size
                if total_size > max_uncompressed_bytes:
                    raise RuntimeError(
                        f"{label}-arkivet blir større enn tillatt grense ved utpakking."
                    )
            validated.append((member, relative))

        for member, relative in validated:
            output_path = destination.joinpath(*relative.parts)
            if member.is_dir():
                ensure_directory_without_links(
                    output_path,
                    label=f"utpakkingsmappe for {label}",
                )
                continue
            ensure_directory_without_links(
                output_path.parent,
                label=f"utpakkingsmappe for {label}",
            )
            flags = (
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            file_descriptor = os.open(output_path, flags, 0o600)
            try:
                with (
                    archive.open(member) as source,
                    os.fdopen(file_descriptor, "wb", closefd=True) as output,
                ):
                    file_descriptor = -1
                    while True:
                        chunk = source.read(DOWNLOAD_CHUNK_BYTES)
                        if not chunk:
                            break
                        output.write(chunk)
            finally:
                if file_descriptor >= 0:
                    os.close(file_descriptor)


def _response_content_length(response: object) -> int | None:
    headers = getattr(response, "headers", None)
    value = headers.get("Content-Length") if headers is not None else None
    if value is None:
        return None
    try:
        length = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Nedlastingen har ugyldig Content-Length: {value!r}") from exc
    if length < 0:
        raise RuntimeError(f"Nedlastingen har ugyldig Content-Length: {value!r}")
    return length


def _require_regular_directory(
    path: Path,
    path_stat: os.stat_result,
    *,
    label: str,
) -> None:
    if (
        stat.S_ISLNK(path_stat.st_mode)
        or is_reparse_stat(path_stat)
        or not stat.S_ISDIR(path_stat.st_mode)
    ):
        raise RuntimeError(
            f"{label} må være en vanlig mappe uten lenker eller reparse points: {path}"
        )


def _validated_zip_member_path(
    member: zipfile.ZipInfo,
    *,
    label: str,
) -> PurePosixPath:
    normalized = member.filename.replace("\\", "/")
    drive, _tail = ntpath.splitdrive(normalized)
    raw_parts = normalized.rstrip("/").split("/")
    if (
        not normalized
        or drive
        or normalized.startswith("/")
        or any(part in {"", ".", ".."} for part in raw_parts)
    ):
        raise RuntimeError(f"{label}-arkivet har en utrygg filsti: {member.filename}")

    mode = member.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise RuntimeError(
            f"{label}-arkivet inneholder en symbolsk lenke: {member.filename}"
        )
    file_type = stat.S_IFMT(mode)
    if file_type and not member.is_dir() and not stat.S_ISREG(mode):
        raise RuntimeError(
            f"{label}-arkivet inneholder en utrygg filtype: {member.filename}"
        )

    for part in raw_parts:
        stem = part.split(".", 1)[0].upper()
        if (
            ":" in part
            or part.endswith((" ", "."))
            or stem in WINDOWS_RESERVED_NAMES
        ):
            raise RuntimeError(
                f"{label}-arkivet har en utrygg Windows-filsti: {member.filename}"
            )
    return PurePosixPath(*raw_parts)
