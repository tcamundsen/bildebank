from __future__ import annotations

import datetime as dt
import os
import socket
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path


LOCK_FILENAME = ".bildebank.lock"
_MAX_LOCK_DETAILS_BYTES = 4096
_WINDOWS_REPARSE_POINT = 0x400


class TargetLockError(RuntimeError):
    pass


class LockFileOwnershipError(RuntimeError):
    pass


@dataclass(frozen=True)
class LockFileState:
    path: Path
    fd: int
    identity: tuple[int, int]
    content: bytes


class TargetLock:
    def __init__(self, target: Path, *, command: str) -> None:
        self.path = target / LOCK_FILENAME
        self.command = command
        self.fd: int | None = None
        self._state: LockFileState | None = None

    def __enter__(self) -> TargetLock:
        owner_id = str(uuid.uuid4())
        created = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        content = (
            f"owner_id={owner_id}\n"
            f"command={self.command}\n"
            f"pid={os.getpid()}\n"
            f"host={socket.gethostname()}\n"
            f"created_at={created}\n"
        ).encode("utf-8")
        try:
            state = create_lock_file(self.path, content)
        except FileExistsError as exc:
            details = lock_details(self.path)
            message = (
                "Bildesamlingen er låst av en annen bildebank-kommando. "
                "Vent til den er ferdig før du kjører kommandoen på nytt."
            )
            if details:
                message = f"{message}\n{details}"
            raise TargetLockError(message) from exc

        self._state = state
        self.fd = state.fd
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        state = self._state
        self._state = None
        self.fd = None
        if state is None:
            return
        try:
            release_lock_file(state)
        except (LockFileOwnershipError, OSError) as cleanup_error:
            message = (
                "Kunne ikke fjerne target-låsen sikkert. Låsfilen er beholdt for å unngå "
                f"å fjerne en annen prosess sin lås: {self.path}: {cleanup_error}"
            )
            if exc is not None:
                exc.add_note(message)
                return
            raise TargetLockError(message) from cleanup_error


def lock_details(path: Path) -> str:
    try:
        content = _read_regular_file(path, max_bytes=_MAX_LOCK_DETAILS_BYTES)
    except (LockFileOwnershipError, OSError):
        return f"Lockfil: {path}"
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return f"Lockfil: {path}"
    text = _escape_control_characters(text).strip()
    if not text:
        return f"Lockfil: {path}"
    return f"Lockfil: {path}\n{text}"


def create_lock_file(path: Path, content: bytes, *, durable: bool = False) -> LockFileState:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    identity: tuple[int, int] | None = None
    try:
        identity = _file_identity(os.fstat(fd))
        _write_all(fd, content)
        if durable:
            os.fsync(fd)
        return LockFileState(path=path, fd=fd, identity=identity, content=content)
    except BaseException as exc:
        if identity is None:
            try:
                os.close(fd)
            except OSError as cleanup_error:
                exc.add_note(f"Kunne ikke lukke ufullstendig låsfil {path}: {cleanup_error}")
        else:
            try:
                _discard_created_lock(path, fd, identity)
            except (LockFileOwnershipError, OSError) as cleanup_error:
                exc.add_note(f"Kunne ikke rydde opp ufullstendig låsfil {path}: {cleanup_error}")
        raise


def release_lock_file(state: LockFileState) -> None:
    os.close(state.fd)
    try:
        content = _read_regular_file(
            state.path,
            max_bytes=len(state.content),
            expected_identity=state.identity,
        )
    except FileNotFoundError:
        return
    if content != state.content:
        raise LockFileOwnershipError("låsfilens innhold er endret")
    try:
        observed = state.path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    _validate_regular_file_identity(observed, state.identity)
    state.path.unlink()


def _discard_created_lock(path: Path, fd: int, identity: tuple[int, int]) -> None:
    try:
        observed = path.stat(follow_symlinks=False)
        _validate_regular_file_identity(observed, identity)
    except FileNotFoundError:
        os.close(fd)
        return
    except BaseException:
        os.close(fd)
        raise
    os.close(fd)
    try:
        observed = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    _validate_regular_file_identity(observed, identity)
    path.unlink()


def _read_regular_file(
    path: Path,
    *,
    max_bytes: int,
    expected_identity: tuple[int, int] | None = None,
) -> bytes:
    before = path.stat(follow_symlinks=False)
    _validate_regular_file_identity(before, expected_identity)
    if before.st_size > max_bytes:
        raise LockFileOwnershipError("låsfilen er større enn tillatt")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        _validate_regular_file_identity(opened, _file_identity(before))
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)

    content = b"".join(chunks)
    if len(content) > max_bytes:
        raise LockFileOwnershipError("låsfilen er større enn tillatt")
    after = path.stat(follow_symlinks=False)
    _validate_regular_file_identity(after, _file_identity(opened))
    return content


def _validate_regular_file_identity(
    observed: os.stat_result,
    expected_identity: tuple[int, int] | None,
) -> None:
    attributes = getattr(observed, "st_file_attributes", 0)
    if attributes & _WINDOWS_REPARSE_POINT or not stat.S_ISREG(observed.st_mode):
        raise LockFileOwnershipError("låsfilen er ikke en vanlig fil uten lenker")
    if expected_identity is not None and _file_identity(observed) != expected_identity:
        raise LockFileOwnershipError("låsfilen er erstattet av en annen fil")


def _file_identity(path_stat: os.stat_result) -> tuple[int, int]:
    return path_stat.st_dev, path_stat.st_ino


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("Kunne ikke skrive alle byte til låsfilen.")
        view = view[written:]


def _escape_control_characters(content: str) -> str:
    parts: list[str] = []
    for character in content:
        if character in "\n\t" or character.isprintable():
            parts.append(character)
        elif ord(character) <= 0xFF:
            parts.append(f"\\x{ord(character):02x}")
        else:
            parts.append(f"\\u{ord(character):04x}")
    return "".join(parts)
