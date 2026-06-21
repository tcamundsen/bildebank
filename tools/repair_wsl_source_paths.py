#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ntpath
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PureWindowsPath

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ruff: noqa: E402
from bildebank import db
from bildebank.media import sha256_file
from bildebank.target_lock import TargetLock


WSL_PATH_RE = re.compile(r"^[\\/]+mnt[\\/]([A-Za-z])(?:[\\/](.*))?$", re.IGNORECASE)
WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


@dataclass(frozen=True)
class SourceFileRow:
    id: int
    file_id: int
    source_path: str
    source_path_key: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class RepairAction:
    kind: str
    row_id: int
    source_path: str
    windows_path: str
    windows_path_key: str
    file_id: int
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class RepairPlan:
    source_id: int
    source_name: str
    source_path: str
    source_path_update: tuple[str, str] | None
    total_rows: int
    wsl_rows: int
    actions: tuple[RepairAction, ...]

    @property
    def delete_count(self) -> int:
        return sum(action.kind == "delete_duplicate" for action in self.actions)

    @property
    def update_count(self) -> int:
        return sum(action.kind == "update_path" for action in self.actions)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        target = args.target.resolve()
        require_windows()
        plan = load_repair_plan(target, args.name)
        validate_source_files(plan)
        print_plan(plan, apply=args.apply)
        if not plan.actions and plan.source_path_update is None:
            print("Ingen WSL-stier trenger reparasjon.")
            return 0
        if not args.apply:
            print("Dry-run: databasen og bildefilene er ikke endret.")
            return 0
        with TargetLock(target, command="repair-wsl-source-paths"):
            conn = db.connect(target)
            try:
                current_plan = build_repair_plan(conn, args.name)
                validate_source_files(current_plan)
                backup_path = backup_database(conn, target)
                conn.execute("BEGIN IMMEDIATE")
                apply_repair_plan(conn, current_plan)
                validate_repair(conn, current_plan.source_id)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        print(f"Reparasjon gjennomført. Databasebackup: {backup_path}")
        print(f"Kjør nå: bildebank unimport --dry-run --name {quote_arg(args.name)}")
        return 0
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        print(f"FEIL: {exc}", file=sys.stderr)
        return 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reparer WSL-stier i file_sources for én importert kilde. "
            "Verktøyet er dry-run som standard og endrer aldri bildefiler."
        )
    )
    parser.add_argument("--target", type=Path, required=True, help="Bildesamlingsmappe.")
    parser.add_argument("--name", required=True, help="Kildenavn fra bildebank list-sources.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Utfør reparasjonen etter kontroll og lag en databasebackup.",
    )
    return parser.parse_args(argv)


def require_windows() -> None:
    if os.name != "nt":
        raise RuntimeError(
            "Dette reparasjonsverktøyet må kjøres direkte i Windows, ikke fra WSL. "
            "Det må kunne kontrollere de konverterte C:\\-kildefilene."
        )


def load_repair_plan(target: Path, source_name: str) -> RepairPlan:
    if not db.db_path_for_target(target).is_file():
        raise ValueError(f"Bildesamlingen er ikke initialisert: {target}")
    conn = db.connect(target)
    try:
        return build_repair_plan(conn, source_name)
    finally:
        conn.close()


def build_repair_plan(conn: sqlite3.Connection, source_name: str) -> RepairPlan:
    source = conn.execute(
        "SELECT id, name, path, path_key FROM sources WHERE name = ?",
        (source_name,),
    ).fetchone()
    if source is None:
        raise ValueError(f"Fant ikke kilde med navn: {source_name}")
    source_id = int(source["id"])
    rows = [
        SourceFileRow(
            id=int(row["id"]),
            file_id=int(row["file_id"]),
            source_path=str(row["source_path"]),
            source_path_key=str(row["source_path_key"]),
            sha256=str(row["sha256"]),
            size_bytes=int(row["size_bytes"]),
        )
        for row in conn.execute(
            """
            SELECT id, file_id, source_path, source_path_key, sha256, size_bytes
            FROM file_sources
            WHERE source_id = ?
            ORDER BY id
            """,
            (source_id,),
        )
    ]
    groups: dict[str, list[tuple[SourceFileRow, bool, str]]] = {}
    for row in rows:
        windows_path = windows_path_for_source_path(row.source_path)
        if windows_path is None:
            continue
        is_wsl = wsl_path_to_windows(row.source_path) is not None
        groups.setdefault(windows_path_key(windows_path), []).append((row, is_wsl, windows_path))

    actions: list[RepairAction] = []
    wsl_rows = 0
    for key, group in groups.items():
        wsl_group = [item for item in group if item[1]]
        if not wsl_group:
            continue
        wsl_rows += len(wsl_group)
        windows_group = [item for item in group if not item[1]]
        if len(windows_group) > 1:
            raise ValueError(f"Flere Windows-rader peker på samme kildefil: {windows_group[0][2]}")
        authoritative = windows_group[0] if windows_group else wsl_group[0]
        authoritative_row, _is_wsl, windows_path = authoritative
        for row, is_wsl, row_windows_path in group:
            if not is_wsl:
                continue
            require_matching_file_metadata(authoritative_row, row, row_windows_path)
            kind = "update_path" if row.id == authoritative_row.id and not windows_group else "delete_duplicate"
            actions.append(
                RepairAction(
                    kind=kind,
                    row_id=row.id,
                    source_path=row.source_path,
                    windows_path=windows_path,
                    windows_path_key=key,
                    file_id=row.file_id,
                    sha256=row.sha256,
                    size_bytes=row.size_bytes,
                )
            )

    raw_source_path = str(source["path"])
    converted_source_path = wsl_path_to_windows(raw_source_path)
    source_path_update = (
        (converted_source_path, windows_path_key(converted_source_path))
        if converted_source_path is not None
        else None
    )
    return RepairPlan(
        source_id=source_id,
        source_name=str(source["name"]),
        source_path=raw_source_path,
        source_path_update=source_path_update,
        total_rows=len(rows),
        wsl_rows=wsl_rows,
        actions=tuple(actions),
    )


def windows_path_for_source_path(value: str) -> str | None:
    converted = wsl_path_to_windows(value)
    if converted is not None:
        return converted
    if WINDOWS_PATH_RE.match(value):
        return normalize_windows_path(value)
    return None


def wsl_path_to_windows(value: str) -> str | None:
    match = WSL_PATH_RE.match(value.strip())
    if match is None:
        return None
    drive = match.group(1).upper()
    tail = match.group(2) or ""
    parts = [part for part in re.split(r"[\\/]+", tail) if part]
    return str(PureWindowsPath(f"{drive}:\\", *parts))


def normalize_windows_path(value: str) -> str:
    return ntpath.normpath(str(PureWindowsPath(value)))


def windows_path_key(value: str) -> str:
    return normalize_windows_path(value).casefold()


def require_matching_file_metadata(reference: SourceFileRow, candidate: SourceFileRow, path: str) -> None:
    mismatches = []
    for field in ("file_id", "sha256", "size_bytes"):
        if getattr(reference, field) != getattr(candidate, field):
            mismatches.append(
                f"{field}: Windows/valgt={getattr(reference, field)!r}, WSL={getattr(candidate, field)!r}"
            )
    if mismatches:
        raise ValueError(f"Kan ikke slå sammen motstridende rader for {path}: " + "; ".join(mismatches))


def validate_source_files(plan: RepairPlan) -> None:
    expected_by_path: dict[str, tuple[int, str]] = {}
    for action in plan.actions:
        expected = (action.size_bytes, action.sha256)
        previous = expected_by_path.setdefault(action.windows_path, expected)
        if previous != expected:
            raise ValueError(f"Motstridende metadata for kildefil: {action.windows_path}")
    for raw_path, (size_bytes, expected_hash) in expected_by_path.items():
        path = Path(raw_path)
        if not path.is_file():
            raise ValueError(f"Konvertert Windows-kildefil finnes ikke: {path}")
        actual_size = path.stat().st_size
        if actual_size != size_bytes:
            raise ValueError(
                f"Kildefilen har endret størrelse: {path} (nå {actual_size}, forventet {size_bytes})"
            )
        actual_hash = sha256_file(path)
        if actual_hash != expected_hash:
            raise ValueError(f"Kildefilen har endret innhold: {path}")
    if plan.source_path_update is not None and not Path(plan.source_path_update[0]).is_dir():
        raise ValueError(f"Konvertert Windows-kildemappe finnes ikke: {plan.source_path_update[0]}")


def backup_database(conn: sqlite3.Connection, target: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = target / f"{db.DB_FILENAME}.backup-before-wsl-source-repair-{stamp}"
    if backup_path.exists():
        raise ValueError(f"Databasebackup finnes allerede: {backup_path}")
    backup_conn = sqlite3.connect(backup_path)
    try:
        conn.backup(backup_conn)
    finally:
        backup_conn.close()
    return backup_path


def apply_repair_plan(conn: sqlite3.Connection, plan: RepairPlan) -> None:
    for action in plan.actions:
        if action.kind == "delete_duplicate":
            conn.execute("DELETE FROM file_sources WHERE id = ?", (action.row_id,))
        elif action.kind == "update_path":
            conn.execute(
                """
                UPDATE file_sources
                SET source_path = ?, source_path_key = ?
                WHERE id = ?
                """,
                (action.windows_path, action.windows_path_key, action.row_id),
            )
        else:
            raise ValueError(f"Ukjent reparasjonshandling: {action.kind}")
    if plan.source_path_update is not None:
        source_path, source_path_key = plan.source_path_update
        conn.execute(
            "UPDATE sources SET path = ?, path_key = ? WHERE id = ?",
            (source_path, source_path_key, plan.source_id),
        )


def validate_repair(conn: sqlite3.Connection, source_id: int) -> None:
    remaining = conn.execute(
        """
        SELECT source_path
        FROM file_sources
        WHERE source_id = ?
          AND (
            source_path LIKE '/mnt/%'
            OR source_path LIKE '\\mnt\\%'
          )
        LIMIT 1
        """,
        (source_id,),
    ).fetchone()
    if remaining is not None:
        raise ValueError(f"WSL-sti står igjen etter reparasjon: {remaining['source_path']}")
    foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise ValueError(f"foreign_key_check feilet etter reparasjon: {foreign_key_errors}")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or integrity[0] != "ok":
        raise ValueError(f"integrity_check feilet etter reparasjon: {integrity[0] if integrity else 'ukjent'}")


def print_plan(plan: RepairPlan, *, apply: bool) -> None:
    print("Reparasjon av WSL-kildestier")
    print(f"  kilde: {plan.source_name} (id={plan.source_id})")
    print(f"  registrerte file_sources: {plan.total_rows}")
    print(f"  WSL-rader funnet: {plan.wsl_rows}")
    print(f"  verifiserte WSL-duplikater som fjernes: {plan.delete_count}")
    print(f"  WSL-rader som konverteres til Windows-sti: {plan.update_count}")
    if plan.source_path_update is not None:
        print(f"  sources.path konverteres: {plan.source_path} -> {plan.source_path_update[0]}")
    print(f"  handling: {'UTFØR' if apply else 'DRY-RUN'}")


def quote_arg(value: str) -> str:
    return f'"{value}"' if any(char.isspace() for char in value) else value


if __name__ == "__main__":
    raise SystemExit(main())
