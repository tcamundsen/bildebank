from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import db
from .config import AppConfig
from .face import face_database_dir, face_model_db_filename
from .importer import safe_copy
from .server_browser import browser_date_for_item, source_items
from .server_browser_sources import person_browser_source
from .server_faces import person_by_name
from .target_lock import TargetLock


WINDOWS_INVALID_NAME_CHARS = frozenset('<>:"/\\|?*')
WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


@dataclass(frozen=True)
class PersonExportEntry:
    source: Path
    destination: Path
    expected_hash: str


@dataclass(frozen=True)
class PersonExportPlan:
    person_name: str
    destination: Path
    entries: tuple[PersonExportEntry, ...]


class PersonExportInterrupted(KeyboardInterrupt):
    pass


def export_person(
    target: Path,
    person_name: str,
    destination_root: Path,
    *,
    config: AppConfig,
    dry_run: bool = False,
) -> PersonExportPlan:
    destination_root = validate_destination_root(destination_root)
    canonical_name = canonical_person_name(target, person_name, config)
    validate_windows_folder_name(canonical_name)
    destination = destination_root / canonical_name
    validate_export_destination(target, destination)

    if dry_run:
        return build_export_plan(target, canonical_name, destination, config)

    with TargetLock(target, command="export-person"):
        validate_export_destination(target, destination)
        plan = build_export_plan(target, canonical_name, destination, config)
        copy_export_plan(plan)
        return plan


def canonical_person_name(target: Path, person_name: str, config: AppConfig) -> str:
    face_config = config.face_recognition
    database_path = face_database_dir(target, face_config) / face_model_db_filename(face_config.model_name)
    if not database_path.exists():
        raise ValueError(f"Fant ikke person: {person_name.strip()}")
    person = person_by_name(target, person_name, face_config)
    if person is None:
        raise ValueError(f"Fant ikke person: {person_name.strip()}")
    return str(person["name"])


def validate_destination_root(destination_root: Path) -> Path:
    destination_root = destination_root.expanduser().resolve()
    if not destination_root.exists():
        raise ValueError(f"Destinasjonsmappen finnes ikke: {destination_root}")
    if not destination_root.is_dir():
        raise ValueError(f"Destinasjonen er ikke en mappe: {destination_root}")
    return destination_root


def validate_export_destination(target: Path, destination: Path) -> None:
    if destination.exists():
        raise ValueError(f"Personmappen finnes allerede: {destination}")
    target_resolved = target.resolve()
    destination_resolved = destination.resolve()
    if paths_overlap(target_resolved, destination_resolved):
        raise ValueError(f"Eksportmappen kan ikke overlappe bildesamlingen: {destination_resolved}")


def paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def validate_windows_folder_name(name: str) -> None:
    if (
        not name
        or name.endswith((" ", "."))
        or any(character in WINDOWS_INVALID_NAME_CHARS or ord(character) < 32 for character in name)
    ):
        raise ValueError(f"Personnavnet er ikke et gyldig Windows-mappenavn: {name!r}")
    if name.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        raise ValueError(f"Personnavnet er et reservert Windows-mappenavn: {name!r}")


def build_export_plan(
    target: Path,
    person_name: str,
    destination: Path,
    config: AppConfig,
) -> PersonExportPlan:
    source = person_browser_source(person_name, include_suggestions=True, show_faces=False)
    items = source_items(
        target,
        source,
        config.face_recognition,
        hide_out_of_focus=config.browser.hide_out_of_focus,
    )
    if not items:
        raise ValueError(f"Personen har ingen synlige bilder: {person_name}")

    hashes = file_hashes(target, [int(item["id"]) for item in items])
    used_paths: set[str] = set()
    entries: list[PersonExportEntry] = []
    for item in items:
        file_id = int(item["id"])
        date = valid_export_date(browser_date_for_item(item))
        directory = Path("udatert") if date is None else Path(f"{date.year:04d}", f"{date.month:02d}")
        filename = collision_free_filename(directory, str(item["stored_filename"]), used_paths)
        source_path = db.absolute_target_path(target, Path(str(item["target_path"])))
        entries.append(
            PersonExportEntry(
                source=source_path,
                destination=destination / directory / filename,
                expected_hash=hashes[file_id],
            )
        )
    return PersonExportPlan(person_name, destination, tuple(entries))


def file_hashes(target: Path, file_ids: list[int]) -> dict[int, str]:
    placeholders = ",".join("?" for _ in file_ids)
    conn = db.connect(target)
    try:
        rows = conn.execute(f"SELECT id, sha256 FROM files WHERE id IN ({placeholders})", file_ids)
        hashes = {int(row["id"]): str(row["sha256"]) for row in rows}
    finally:
        conn.close()
    if len(hashes) != len(file_ids):
        raise ValueError("Eksportutvalget endret seg mens eksportplanen ble laget.")
    return hashes


def collision_free_filename(directory: Path, filename: str, used_paths: set[str]) -> str:
    candidate = filename
    index = 0
    while str(directory / candidate).casefold() in used_paths:
        index += 1
        path = Path(filename)
        candidate = f"{path.stem}-{index}{path.suffix}"
    used_paths.add(str(directory / candidate).casefold())
    return candidate


def valid_export_date(value: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def copy_export_plan(plan: PersonExportPlan) -> None:
    temporary = plan.destination.with_name(
        f".bildebank-export-person-{plan.person_name}-incomplete-{uuid.uuid4().hex}"
    )
    temporary.mkdir()
    try:
        for entry in plan.entries:
            relative_destination = entry.destination.relative_to(plan.destination)
            temporary_destination = temporary / relative_destination
            temporary_destination.parent.mkdir(parents=True, exist_ok=True)
            safe_copy(entry.source, temporary_destination, entry.expected_hash)
        temporary.rename(plan.destination)
    except KeyboardInterrupt as exc:
        raise PersonExportInterrupted(
            f"Eksporten ble avbrutt. Ufullstendig eksport er beholdt i: {temporary}"
        ) from exc
    except Exception as exc:
        raise RuntimeError(
            f"Eksporten feilet. Ufullstendig eksport er beholdt i: {temporary}. {exc}"
        ) from exc
