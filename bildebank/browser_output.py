from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path

from .collection_paths import is_reparse_stat


BROWSER_DIRECTORY_NAME = "browser"
BROWSER_MARKER_FILENAME = ".bildebank-generated.json"
BROWSER_FORMAT_VERSION = 1
BROWSER_INDEX_FILENAME = "index.html"
PEOPLE_DIRECTORY_NAME = "people"
PEOPLE_INDEX_FILENAME = "index.html"
PERSON_PAGE_FILENAME_PREFIX = "person-"
PERSON_PAGE_FILENAME_SUFFIX = ".html"
PEOPLE_PREVIOUS_DIRECTORY_NAME = ".people.previous"
PEOPLE_STAGING_DIRECTORY_PREFIX = ".people.incomplete-"
MAX_MARKER_BYTES = 4096


def browser_root(target: Path) -> Path:
    return target / BROWSER_DIRECTORY_NAME


def browser_index_path(target: Path) -> Path:
    return browser_root(target) / BROWSER_INDEX_FILENAME


def people_root(target: Path) -> Path:
    return browser_root(target) / PEOPLE_DIRECTORY_NAME


def people_index_path(target: Path) -> Path:
    return people_root(target) / PEOPLE_INDEX_FILENAME


def person_page_filename(person_id: int) -> str:
    if person_id <= 0:
        raise ValueError(f"Ugyldig person-ID for statisk browser: {person_id}")
    return f"{PERSON_PAGE_FILENAME_PREFIX}{person_id}{PERSON_PAGE_FILENAME_SUFFIX}"


def person_page_path(target: Path, person_id: int) -> Path:
    return people_root(target) / person_page_filename(person_id)


def publish_browser_index(target: Path, content: str) -> Path:
    root = ensure_browser_root(target)
    output_path = root / BROWSER_INDEX_FILENAME
    replace_text_file(output_path, content)
    return output_path


def publish_person_page(target: Path, person_id: int, content: str) -> Path:
    directory = ensure_people_root(target)
    output_path = directory / person_page_filename(person_id)
    replace_text_file(output_path, content)
    return output_path


def create_people_staging_directory(target: Path) -> Path:
    root = ensure_browser_root(target)
    recover_people_publication(root)
    staging = root / f"{PEOPLE_STAGING_DIRECTORY_PREFIX}{uuid.uuid4().hex}"
    staging.mkdir()
    return staging


def publish_people_directory(target: Path, staging: Path) -> Path:
    root = ensure_browser_root(target)
    _require_direct_child(staging, root, prefix=PEOPLE_STAGING_DIRECTORY_PREFIX)
    _require_directory_without_links(staging, label="Stagingmappen for personbrowseren")

    destination = root / PEOPLE_DIRECTORY_NAME
    previous = root / PEOPLE_PREVIOUS_DIRECTORY_NAME
    moved_previous = False
    try:
        destination_stat = _path_stat(destination)
        if destination_stat is not None:
            _require_directory_stat_without_links(
                destination,
                destination_stat,
                label="Personbrowsermappen",
            )
            destination.rename(previous)
            moved_previous = True
        staging.rename(destination)
    except BaseException as exc:
        if moved_previous and _path_stat(destination) is None and _path_stat(previous) is not None:
            try:
                previous.rename(destination)
            except OSError as rollback_error:
                exc.add_note(
                    "Kunne ikke rulle tilbake personbrowsermappen. "
                    f"Tidligere generasjon ligger i {previous}: {rollback_error}"
                )
        raise

    if moved_previous:
        try:
            remove_generated_tree(previous)
        except Exception as exc:
            raise RuntimeError(
                "Ny personbrowser er publisert, men den forrige generasjonen "
                f"kunne ikke fjernes: {previous}: {exc}"
            ) from exc
    return destination


def discard_people_staging_directory(staging: Path) -> None:
    if _path_stat(staging) is None:
        return
    remove_generated_tree(staging)


def ensure_browser_root(target: Path) -> Path:
    _require_directory_without_links(target, label="Bildesamlingen")
    root = browser_root(target)
    root_stat = _path_stat(root)
    if root_stat is None:
        root.mkdir()
        try:
            write_new_text_file(root / BROWSER_MARKER_FILENAME, _browser_marker_text())
        except BaseException:
            try:
                root.rmdir()
            except OSError:
                pass
            raise
        return root

    _require_directory_stat_without_links(root, root_stat, label="Browsermappen")
    validate_browser_marker(root / BROWSER_MARKER_FILENAME)
    return root


def ensure_people_root(target: Path) -> Path:
    root = ensure_browser_root(target)
    recover_people_publication(root)
    directory = root / PEOPLE_DIRECTORY_NAME
    directory_stat = _path_stat(directory)
    if directory_stat is None:
        directory.mkdir()
        return directory
    _require_directory_stat_without_links(
        directory,
        directory_stat,
        label="Personbrowsermappen",
    )
    return directory


def validate_browser_marker(marker: Path) -> None:
    marker_stat = _path_stat(marker)
    if marker_stat is None:
        raise ValueError(
            f"Browsermappen mangler Bildebanks eierskapsmarkør: {marker}"
        )
    if (
        stat.S_ISLNK(marker_stat.st_mode)
        or is_reparse_stat(marker_stat)
        or not stat.S_ISREG(marker_stat.st_mode)
    ):
        raise ValueError(
            f"Browsermarkøren er ikke en vanlig fil uten lenker: {marker}"
        )
    try:
        value = json.loads(_read_small_regular_file(marker, marker_stat))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Browsermarkøren kunne ikke leses: {marker}: {exc}") from exc
    if value != {
        "created_by": "bildebank",
        "format_version": BROWSER_FORMAT_VERSION,
    }:
        raise ValueError(
            f"Browsermappen har en ukjent eller ugyldig eierskapsmarkør: {marker}"
        )


def recover_people_publication(root: Path) -> None:
    _require_directory_without_links(root, label="Browsermappen")
    destination = root / PEOPLE_DIRECTORY_NAME
    previous = root / PEOPLE_PREVIOUS_DIRECTORY_NAME
    previous_stat = _path_stat(previous)
    destination_stat = _path_stat(destination)
    if previous_stat is not None:
        _require_directory_stat_without_links(
            previous,
            previous_stat,
            label="Forrige personbrowsergenerasjon",
        )
        if destination_stat is None:
            previous.rename(destination)
            destination_stat = _path_stat(destination)
        else:
            _require_directory_stat_without_links(
                destination,
                destination_stat,
                label="Personbrowsermappen",
            )
            remove_generated_tree(previous)

    for entry in list(root.iterdir()):
        if not entry.name.startswith(PEOPLE_STAGING_DIRECTORY_PREFIX):
            continue
        remove_generated_tree(entry)


def validate_legacy_browser_index(target: Path) -> None:
    _validate_legacy_file(target / BROWSER_INDEX_FILENAME)


def cleanup_legacy_browser_index(target: Path) -> None:
    _remove_legacy_file(target / BROWSER_INDEX_FILENAME)


def validate_legacy_people_files(target: Path) -> None:
    for path in legacy_people_paths(target):
        _validate_legacy_file(path)


def cleanup_legacy_people_files(target: Path) -> None:
    for path in legacy_people_paths(target):
        _remove_legacy_file(path)


def legacy_people_paths(target: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    people_index = target / "personer.html"
    if _path_stat(people_index) is not None:
        paths.append(people_index)
    for entry in target.iterdir():
        name = entry.name.casefold()
        if name.startswith("person-") and name.endswith(".html"):
            paths.append(entry)
    return tuple(sorted(set(paths), key=lambda path: path.name.casefold()))


def replace_text_file(path: Path, content: str) -> None:
    _require_directory_without_links(path.parent, label="Browserens målmappe")
    _require_replaceable_file(path)
    candidate = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        _write_exclusive_file(candidate, content.encode("utf-8"))
        _require_directory_without_links(path.parent, label="Browserens målmappe")
        _require_replaceable_file(path)
        os.replace(candidate, path)
    except BaseException:
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass
        raise


def write_new_text_file(path: Path, content: str) -> None:
    _require_directory_without_links(path.parent, label="Browserens målmappe")
    if _path_stat(path) is not None:
        raise FileExistsError(f"Browserfilen finnes allerede: {path}")
    _write_exclusive_file(path, content.encode("utf-8"))


def remove_generated_tree(path: Path) -> None:
    path_stat = _path_stat(path)
    if path_stat is None:
        return
    _require_directory_stat_without_links(
        path,
        path_stat,
        label="Generert browsermappe",
    )
    for entry in list(path.iterdir()):
        entry_stat = _path_stat(entry)
        if entry_stat is None:
            continue
        if stat.S_ISLNK(entry_stat.st_mode) or is_reparse_stat(entry_stat):
            raise ValueError(
                f"Generert browsermappe inneholder en lenke eller et reparse point: {entry}"
            )
        if stat.S_ISDIR(entry_stat.st_mode):
            remove_generated_tree(entry)
        elif stat.S_ISREG(entry_stat.st_mode):
            entry.unlink()
        else:
            raise ValueError(
                f"Generert browsermappe inneholder en ukjent filtype: {entry}"
            )
    path.rmdir()


def _browser_marker_text() -> str:
    return (
        json.dumps(
            {
                "created_by": "bildebank",
                "format_version": BROWSER_FORMAT_VERSION,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
    )


def _read_small_regular_file(path: Path, before: os.stat_result) -> str:
    if before.st_size > MAX_MARKER_BYTES:
        raise OSError("filen er større enn tillatt")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_descriptor = os.open(path, flags)
    try:
        opened = os.fstat(file_descriptor)
        _require_same_regular_file(before, opened)
        content = os.read(file_descriptor, MAX_MARKER_BYTES + 1)
    finally:
        os.close(file_descriptor)
    if len(content) > MAX_MARKER_BYTES:
        raise OSError("filen er større enn tillatt")
    after = path.stat(follow_symlinks=False)
    _require_same_regular_file(opened, after)
    return content.decode("utf-8")


def _write_exclusive_file(path: Path, content: bytes) -> None:
    flags = (
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_descriptor = os.open(path, flags, 0o600)
    try:
        remaining = memoryview(content)
        while remaining:
            written = os.write(file_descriptor, remaining)
            if written <= 0:
                raise OSError("Kunne ikke skrive hele browserfilen.")
            remaining = remaining[written:]
        os.fsync(file_descriptor)
    except BaseException:
        os.close(file_descriptor)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    os.close(file_descriptor)


def _require_replaceable_file(path: Path) -> None:
    path_stat = _path_stat(path)
    if path_stat is None:
        return
    if (
        stat.S_ISLNK(path_stat.st_mode)
        or is_reparse_stat(path_stat)
        or not stat.S_ISREG(path_stat.st_mode)
    ):
        raise ValueError(
            f"Browserens målfil er ikke en vanlig fil uten lenker: {path}"
        )


def _validate_legacy_file(path: Path) -> None:
    path_stat = _path_stat(path)
    if path_stat is None:
        return
    if (
        stat.S_ISLNK(path_stat.st_mode)
        or is_reparse_stat(path_stat)
        or not stat.S_ISREG(path_stat.st_mode)
    ):
        raise ValueError(
            f"Eldre browserfil er ikke en vanlig fil uten lenker og fjernes ikke: {path}"
        )


def _remove_legacy_file(path: Path) -> None:
    _validate_legacy_file(path)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _require_directory_without_links(path: Path, *, label: str) -> None:
    path_stat = _path_stat(path)
    if path_stat is None:
        raise ValueError(f"{label} finnes ikke: {path}")
    _require_directory_stat_without_links(path, path_stat, label=label)


def _require_directory_stat_without_links(
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
        raise ValueError(f"{label} er ikke en vanlig mappe uten lenker: {path}")


def _require_same_regular_file(
    expected: os.stat_result,
    observed: os.stat_result,
) -> None:
    if (
        stat.S_ISLNK(observed.st_mode)
        or is_reparse_stat(observed)
        or not stat.S_ISREG(observed.st_mode)
        or expected.st_dev != observed.st_dev
        or expected.st_ino != observed.st_ino
    ):
        raise OSError("filen ble erstattet under lesing")


def _require_direct_child(path: Path, parent: Path, *, prefix: str) -> None:
    if path.parent != parent or not path.name.startswith(prefix):
        raise ValueError(f"Ugyldig stagingmappe for statisk browser: {path}")


def _path_stat(path: Path) -> os.stat_result | None:
    try:
        return path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"Kunne ikke kontrollere browsersti: {path}: {exc}") from exc
