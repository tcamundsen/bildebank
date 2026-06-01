from __future__ import annotations

import datetime as dt
import re
import shutil
from pathlib import Path

from . import db
from .geo import h3_cells_for_manual_cell
from .target_lock import TargetLock


def rotate_file_view(target: Path, file_id: int, direction: str) -> int:
    conn = db.connect(target)
    try:
        rotation = db.rotate_file_view(conn, file_id, direction)
        conn.commit()
        return rotation
    finally:
        conn.close()


def set_system_tag_on_file(target: Path, file_id: int, tag_name: str, tagged: bool) -> bool:
    if not db.is_system_tag_name(tag_name):
        raise ValueError("Webtagging støtter foreløpig bare systemtagger.")
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
        db.set_file_tag(conn, file_id=file_id, tag_name=tag_name, tagged=tagged)
        conn.commit()
        return tagged
    finally:
        conn.close()


def set_manual_h3_location_on_file(target: Path, file_id: int, h3_cell: str) -> None:
    clean_h3_cell = h3_cell.strip()
    if not clean_h3_cell:
        raise ValueError("Aktiv manuell H3-celle er ikke satt.")
    cells = h3_cells_for_manual_cell(clean_h3_cell)
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


def date_range_from_uncertainty(center: dt.date, value: str) -> tuple[dt.date, dt.date]:
    match = re.fullmatch(r"\s*(\d+)\s*([A-Za-zæøåÆØÅ]+)\s*", value)
    if match is None:
        raise ValueError("Ugyldig usikkerhet. Bruk for eksempel 3d, 2w, 1m eller 1y.")
    amount = int(match.group(1))
    if amount < 1:
        raise ValueError("Usikkerhet må være minst 1.")
    unit = match.group(2).lower()
    if unit in {"d", "day", "days", "dag", "dager"}:
        delta = dt.timedelta(days=amount)
        return center - delta, center + delta
    if unit in {"w", "week", "weeks", "uke", "uker"}:
        delta = dt.timedelta(weeks=amount)
        return center - delta, center + delta
    if unit in {"m", "month", "months", "måned", "måneder"}:
        return add_months(center, -amount), add_months(center, amount)
    if unit in {"y", "year", "years", "år"}:
        return add_months(center, -12 * amount), add_months(center, 12 * amount)
    raise ValueError("Ugyldig usikkerhetsenhet. Bruk d, w, m eller y.")


def add_months(value: dt.date, months: int) -> dt.date:
    month_index = value.year * 12 + value.month - 1 + months
    year = month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, days_in_month(year, month))
    return dt.date(year, month, day)


def days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (dt.date(year, month + 1, 1) - dt.timedelta(days=1)).day


def remove_file_from_browser(target: Path, file_id: int) -> Path:
    with TargetLock(target, command="remove"):
        conn = db.connect(target)
        try:
            row = conn.execute(
                """
                SELECT id, target_path, deleted_at
                FROM files
                WHERE id = ?
                """,
                (file_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Filen finnes ikke i importdatabasen.")
            if row["deleted_at"] is not None:
                raise ValueError("Filen er allerede markert som slettet.")

            original_path = db.absolute_target_path(target, Path(str(row["target_path"]))).resolve()
            if not original_path.exists():
                raise ValueError(f"Målfilen finnes ikke på disk: {original_path}")
            try:
                relative_path = original_path.relative_to(target.resolve())
            except ValueError as exc:
                raise ValueError(f"Filen ligger ikke i bildesamlingen: {original_path}") from exc
            if not relative_path.parts or relative_path.parts[0] == "deleted":
                raise ValueError(f"Kan ikke slette filer fra deleted/: {original_path}")

            deleted_path = target / "deleted" / relative_path
            if deleted_path.exists():
                raise ValueError(f"Slettemål finnes allerede: {deleted_path}")

            deleted_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(original_path), str(deleted_path))
            db.mark_file_deleted(
                conn,
                file_id=file_id,
                target_root=target,
                deleted_path=deleted_path,
                original_target_path=original_path,
            )
            conn.commit()
            return db.target_relative_path(target, deleted_path)
        finally:
            conn.close()


def undelete_file_from_browser(target: Path, file_id: int) -> Path:
    with TargetLock(target, command="undelete"):
        conn = db.connect(target)
        try:
            row = conn.execute(
                """
                SELECT id, target_path, deleted_at, deleted_original_target_path
                FROM files
                WHERE id = ?
                """,
                (file_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Filen finnes ikke i importdatabasen.")
            if row["deleted_at"] is None:
                raise ValueError("Filen er ikke markert som slettet.")
            if row["deleted_original_target_path"] is None:
                raise ValueError("Filen mangler opprinnelig målsti i databasen.")

            deleted_path = db.absolute_target_path(target, Path(str(row["target_path"]))).resolve()
            if not deleted_path.exists():
                raise ValueError(f"Slettet fil finnes ikke på disk: {deleted_path}")
            try:
                deleted_relative_path = deleted_path.relative_to(target.resolve())
            except ValueError as exc:
                raise ValueError(f"Filen ligger ikke i bildesamlingen: {deleted_path}") from exc
            if len(deleted_relative_path.parts) < 2 or deleted_relative_path.parts[0] != "deleted":
                raise ValueError(f"Slettet fil ligger ikke under deleted/: {deleted_path}")

            restored_path = target / Path(str(row["deleted_original_target_path"]))
            if restored_path.exists():
                raise ValueError(f"Målfilen finnes allerede: {restored_path}")

            restored_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(deleted_path), str(restored_path))
            db.mark_file_undeleted(
                conn,
                file_id=file_id,
                target_root=target,
                restored_path=restored_path,
            )
            conn.commit()
            return db.target_relative_path(target, restored_path)
        finally:
            conn.close()
