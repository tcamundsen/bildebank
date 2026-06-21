from __future__ import annotations

import datetime as dt
from pathlib import Path

from . import db
from .config import BrowserHotkeyConfig, FaceRecognitionConfig
from .face import AddPersonToFileResult, add_person_to_file
from .file_lifecycle import remove_file, undelete_file
from .file_tags import set_file_tag
from .geo import h3_cells_for_manual_cell
from .manual_dates import date_range_from_uncertainty
from .target_lock import TargetLock


def rotate_file_view(target: Path, file_id: int, direction: str) -> int:
    conn = db.connect(target)
    try:
        rotation = db.rotate_file_view(conn, file_id, direction)
        conn.commit()
        return rotation
    finally:
        conn.close()


def set_tag_on_file(target: Path, file_id: int, tag_name: str, tagged: bool) -> bool:
    set_file_tag(target, file_id=file_id, tag_name=tag_name, tagged=tagged)
    return tagged


def set_manual_h3_location_on_file(target: Path, file_id: int, h3_cell: str) -> None:
    clean_h3_cell = h3_cell.strip()
    if not clean_h3_cell:
        raise ValueError("Aktiv manuell H3-celle er ikke satt.")
    cells = h3_cells_for_manual_cell(clean_h3_cell)
    with TargetLock(target, command="manual-h3-set"):
        conn = db.connect(target)
        try:
            if not db.set_file_manual_h3_location(
                conn,
                file_id=file_id,
                h3_cells=cells,
            ):
                raise ValueError("Filen finnes ikke i importdatabasen.")
            conn.commit()
        finally:
            conn.close()


def remove_manual_h3_location_from_file(target: Path, file_id: int) -> None:
    with TargetLock(target, command="manual-h3-remove"):
        conn = db.connect(target)
        try:
            if not db.remove_file_manual_h3_location(conn, file_id=file_id):
                raise ValueError("Filen finnes ikke i importdatabasen.")
            conn.commit()
        finally:
            conn.close()


def set_manual_date_on_file(
    target: Path,
    file_id: int,
    *,
    mode: str,
    date: str = "",
    uncertainty: str = "",
    date_from: str = "",
    date_to: str = "",
    note: str | None = None,
) -> tuple[dt.date, dt.date]:
    start, end = manual_date_range_from_values(
        mode=mode,
        date=date,
        uncertainty=uncertainty,
        date_from=date_from,
        date_to=date_to,
    )
    conn = db.connect(target)
    try:
        row = conn.execute(
            """
            SELECT id, deleted_at
            FROM files
            WHERE id = ?
            """,
            (file_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Filen finnes ikke i importdatabasen.")
        if row["deleted_at"] is not None:
            raise ValueError("Filen er markert som slettet.")
        db.set_manual_date(
            conn,
            file_id=file_id,
            date_from=start.isoformat(),
            date_to=end.isoformat(),
            note=note,
        )
        conn.commit()
        return start, end
    finally:
        conn.close()


def clear_manual_date_on_file(target: Path, file_id: int) -> None:
    conn = db.connect(target)
    try:
        row = conn.execute(
            """
            SELECT id, deleted_at
            FROM files
            WHERE id = ?
            """,
            (file_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Filen finnes ikke i importdatabasen.")
        if row["deleted_at"] is not None:
            raise ValueError("Filen er markert som slettet.")
        db.clear_manual_date(conn, file_id=file_id)
        conn.commit()
    finally:
        conn.close()


def apply_browser_hotkey_to_file(
    target: Path,
    file_id: int,
    hotkey: BrowserHotkeyConfig,
    *,
    face_config: FaceRecognitionConfig | None = None,
) -> dict[str, object]:
    if not hotkey.action:
        raise ValueError("Hurtigtasten er ikke satt.")
    if hotkey.action == "h3":
        set_manual_h3_location_on_file(target, file_id, hotkey.h3_cell)
        return {"action": "h3", "file_id": file_id, "gps_source": "manual-h3"}
    if hotkey.action == "manual_date":
        date_from, date_to = set_manual_date_on_file(
            target,
            file_id,
            mode=hotkey.mode,
            date=hotkey.date,
            uncertainty=hotkey.uncertainty,
            date_from=hotkey.date_from,
            date_to=hotkey.date_to,
            note=hotkey.note,
        )
        return {
            "action": "manual_date",
            "file_id": file_id,
            "manual_date_from": date_from.isoformat(),
            "manual_date_to": date_to.isoformat(),
        }
    if hotkey.action == "person":
        result = add_person_to_file(target, hotkey.person_name, file_id, face_config)
        return hotkey_person_result_payload(result)
    if hotkey.action == "tag":
        set_tag_on_file(target, file_id, hotkey.tag_name, True)
        return {
            "action": "tag",
            "file_id": file_id,
            "tag_name": db.normalize_tag_name(hotkey.tag_name),
            "tagged": True,
        }
    raise ValueError(f"Ukjent hurtigtasthandling: {hotkey.action}")


def hotkey_person_result_payload(result: AddPersonToFileResult) -> dict[str, object]:
    return {
        "action": "person",
        "file_id": result.file_id,
        "person_name": result.person_name,
        "added": result.added,
    }


def manual_date_range_from_values(
    *,
    mode: str,
    date: str = "",
    uncertainty: str = "",
    date_from: str = "",
    date_to: str = "",
) -> tuple[dt.date, dt.date]:
    clean_mode = mode.strip()
    if clean_mode == "exact":
        value = parse_manual_iso_date(date, "Dato")
        return value, value
    if clean_mode == "uncertain":
        value = parse_manual_iso_date(date, "Dato")
        if not uncertainty.strip():
            raise ValueError("Usikkerhet mangler.")
        return date_range_from_uncertainty(value, uncertainty)
    if clean_mode == "between":
        start = parse_manual_iso_date(date_from, "Fra-dato")
        end = parse_manual_iso_date(date_to, "Til-dato")
        if start > end:
            raise ValueError("Fra-dato kan ikke være etter til-dato.")
        return start, end
    raise ValueError("Ugyldig datomodus.")


def parse_manual_iso_date(value: str, label: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{label} må være på formen YYYY-MM-DD.") from exc


def remove_file_from_browser(target: Path, file_id: int) -> Path:
    return remove_file(target, file_id=file_id)


def undelete_file_from_browser(target: Path, file_id: int) -> Path:
    return undelete_file(target, file_id=file_id)
