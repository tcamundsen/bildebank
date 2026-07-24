from __future__ import annotations

import errno
import os
import shutil
import stat
from pathlib import Path

from .media import sha256_file


COPY_CHUNK_SIZE = 1024 * 1024
_HARDLINK_FALLBACK_ERRNOS = {
    errno.EACCES,
    errno.EXDEV,
    errno.EINVAL,
    errno.EMLINK,
    errno.ENOSYS,
    errno.EPERM,
}
if hasattr(errno, "ENOTSUP"):
    _HARDLINK_FALLBACK_ERRNOS.add(errno.ENOTSUP)
if hasattr(errno, "EOPNOTSUPP"):
    _HARDLINK_FALLBACK_ERRNOS.add(errno.EOPNOTSUPP)


def move_file_no_replace(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
) -> None:
    """Move a journaled, pre-verified file without replacing a destination path.

    The destination is verified before the caller completes the database move.
    The caller must have verified the source before committing the pending row.
    """
    source = Path(source)
    destination = Path(destination)
    _require_regular_source(source)
    if _path_entry_exists(destination):
        raise _destination_exists_error(destination)

    if _is_windows():
        _rename_no_replace_windows(source, destination)
        _verify_expected_sha256(destination, expected_sha256)
    else:
        _link_or_copy_no_replace(source, destination, expected_sha256)


def _is_windows() -> bool:
    return os.name == "nt"


def _rename_no_replace_windows(source: Path, destination: Path) -> None:
    try:
        os.rename(source, destination)
    except OSError as exc:
        if _path_entry_exists(destination):
            raise _destination_exists_error(destination) from exc
        raise


def _link_or_copy_no_replace(
    source: Path,
    destination: Path,
    expected_sha256: str,
) -> None:
    try:
        os.link(source, destination, follow_symlinks=False)
    except FileExistsError as exc:
        raise _destination_exists_error(destination) from exc
    except OSError as exc:
        if exc.errno not in _HARDLINK_FALLBACK_ERRNOS:
            raise
        _copy_no_replace(source, destination, expected_sha256)
        return

    _verify_expected_sha256(destination, expected_sha256)
    source.unlink()


def _copy_no_replace(
    source: Path,
    destination: Path,
    expected_sha256: str,
) -> None:
    source_flags = os.O_RDONLY
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        source_flags |= os.O_BINARY
        destination_flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        source_flags |= os.O_NOFOLLOW
        destination_flags |= os.O_NOFOLLOW

    source_fd = os.open(source, source_flags)
    destination_fd = -1
    try:
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise ValueError(f"Flyttekilden er ikke en vanlig fil: {source}")
        try:
            destination_fd = os.open(destination, destination_flags, source_stat.st_mode & 0o777)
        except FileExistsError as exc:
            raise _destination_exists_error(destination) from exc

        with (
            os.fdopen(source_fd, "rb", closefd=True) as source_stream,
            os.fdopen(destination_fd, "wb", closefd=True) as destination_stream,
        ):
            source_fd = -1
            destination_fd = -1
            shutil.copyfileobj(source_stream, destination_stream, length=COPY_CHUNK_SIZE)
            destination_stream.flush()
            os.fsync(destination_stream.fileno())
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if destination_fd >= 0:
            os.close(destination_fd)

    shutil.copystat(source, destination, follow_symlinks=False)
    _verify_expected_sha256(source, expected_sha256)
    _verify_expected_sha256(destination, expected_sha256)
    source.unlink()


def _require_regular_source(source: Path) -> None:
    try:
        source_stat = source.lstat()
    except FileNotFoundError:
        raise FileNotFoundError(f"Flyttekilden finnes ikke: {source}") from None
    if not stat.S_ISREG(source_stat.st_mode):
        raise ValueError(f"Flyttekilden er ikke en vanlig fil: {source}")


def _verify_expected_sha256(path: Path, expected_sha256: str) -> None:
    actual_sha256 = sha256_file(path)
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"Fila på disk har feil SHA-256: {path} "
            f"(forventet {expected_sha256}, fant {actual_sha256})"
        )


def _path_entry_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _destination_exists_error(destination: Path) -> FileExistsError:
    return FileExistsError(
        f"Flyttemålet finnes allerede og blir ikke overskrevet: {destination}"
    )
