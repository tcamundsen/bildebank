from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import db
from .collection_paths import (
    CollectionFileHashError,
    InvalidCollectionRelativePath,
    hash_stable_collection_file,
    inspect_existing_collection_path_components,
    is_active_collection_file_path,
    is_deleted_collection_file_path,
    parse_collection_relative_path,
)
from .importer import WalkError, validate_source_target
from .media import is_supported_media
from .progress import ProgressMeter
from .target_lock import TargetLock


CHECK_SOURCE_PROGRESS: ProgressMeter | None = None
CHECK_SOURCE_MISSING_KIND = "missing"
CHECK_SOURCE_DELETED_KIND = "deleted"
CHECK_SOURCE_MISSING_REASON = "filen er ikke importert i bildesamlingen med samme SHA-256"


@dataclass
class CheckSourceStats:
    scanned: int = 0
    covered: int = 0
    deleted: int = 0
    missing: int = 0
    ignored_json: int = 0
    source_errors: int = 0
    target_errors: int = 0


@dataclass(frozen=True)
class CheckSourceProblem:
    path: Path
    reason: str
    kind: str = "error"


def run_check_source(
    target: Path,
    source_arg: Path,
    *,
    verbose: bool = True,
    accept_deleted: bool = False,
    path_adapter: Callable[[Path], Path] = lambda path: path,
) -> int:
    source = path_adapter(source_arg).resolve()
    if not source.exists() or not source.is_dir():
        raise ValueError(f"Kilden finnes ikke som mappe: {source}")
    validate_source_target(source, target)

    with TargetLock(target, command="check-source"):
        return run_check_source_locked(
            target,
            source,
            verbose=verbose,
            accept_deleted=accept_deleted,
        )


def run_check_source_locked(
    target: Path,
    source: Path,
    *,
    verbose: bool,
    accept_deleted: bool,
) -> int:
    try:
        conn = db.connect_read_only(target, require_current=False)
    except sqlite3.Error as exc:
        print_check_source_database_errors(
            source,
            [f"hoveddatabasen kunne ikke åpnes read-only: {exc}"],
        )
        return 2

    database_errors = check_source_database_errors(target, conn)
    if database_errors:
        conn.close()
        print_check_source_database_errors(source, database_errors)
        return 2

    progress = check_source_progress() if verbose else None
    stats = CheckSourceStats()
    problems: list[CheckSourceProblem] = []
    target_hash_cache: dict[int, bool] = {}
    try:
        if progress is not None:
            progress.message(f"Check-source: leser filoversikt for {source}.")
        source_items = list(iter_check_source_files(source))
        total_files = sum(1 for item in source_items if not isinstance(item, WalkError))
        if progress is not None:
            progress.message(f"Check-source: fant {total_files} filer i {source}.")
        for item in source_items:
            if isinstance(item, WalkError):
                stats.source_errors += 1
                problems.append(CheckSourceProblem(item.path, item.message))
                continue
            path = item
            if is_google_json_sidecar(path):
                stats.ignored_json += 1
                continue
            stats.scanned += 1
            try:
                file_hash, _size_bytes = hash_stable_collection_file(
                    source,
                    path.relative_to(source),
                )
            except (CollectionFileHashError, OSError, ValueError) as exc:
                stats.source_errors += 1
                problems.append(
                    CheckSourceProblem(
                        path,
                        f"kan ikke kontrollere filen stabilt i kilden: {exc}",
                    )
                )
                continue

            rows = db.files_by_hash(conn, file_hash)
            active_rows = [row for row in rows if row["deleted_at"] is None]
            deleted_rows = [row for row in rows if row["deleted_at"] is not None]
            active_valid = bool(active_rows) and check_source_hash_is_validated(target, active_rows, target_hash_cache)
            deleted_valid = bool(deleted_rows) and check_source_hash_is_validated(target, deleted_rows, target_hash_cache)
            if active_valid:
                stats.covered += 1
            elif deleted_rows:
                stats.deleted += 1
                deleted_label = check_source_deleted_target_label(deleted_rows[0])
                if deleted_valid:
                    if not accept_deleted:
                        problems.append(
                            CheckSourceProblem(
                                path,
                                f"filen finnes i bildesamlingen, men er markert slettet: {deleted_label}",
                                CHECK_SOURCE_DELETED_KIND,
                            )
                        )
                else:
                    stats.target_errors += 1
                    problems.append(
                        CheckSourceProblem(
                            path,
                            f"filen er markert slettet, men deleted/-filen mangler eller har endret innhold: "
                            f"{deleted_label}",
                            CHECK_SOURCE_DELETED_KIND,
                        )
                    )
            elif not rows:
                stats.missing += 1
                problems.append(CheckSourceProblem(path, CHECK_SOURCE_MISSING_REASON, CHECK_SOURCE_MISSING_KIND))
            else:
                stats.target_errors += 1
                problems.append(CheckSourceProblem(path, "matchende fil i bildesamlingen mangler eller har endret innhold"))

            if progress is not None:
                progress.update(
                    stats.scanned,
                    total_files,
                    action="kontrollert",
                    details=check_source_progress_details(stats),
                    eta=True,
                )

        if progress is not None:
            progress.message(
                f"Check-source: leser filoversikt på nytt for {source}."
            )
        final_source_items = list(iter_check_source_files(source))
        inventory_problems = check_source_inventory_problems(
            source_items,
            final_source_items,
        )
        stats.source_errors += len(inventory_problems)
        problems.extend(inventory_problems)
        if progress is not None:
            progress.done()
    finally:
        conn.close()

    problem_report_path = write_check_source_problem_report(problems) if problems else None
    print_check_source_report(source, stats, problems, problem_report_path=problem_report_path)
    if problem_report_path is not None:
        open_check_source_missing_report(problem_report_path)
    return 0 if check_source_is_safe(stats, accept_deleted=accept_deleted) else 2


def check_source_deleted_target_label(row) -> str:
    return Path(str(row["target_path"])).as_posix()


def write_check_source_problem_report(problems: list[CheckSourceProblem]) -> Path:
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        prefix="bildebank-check-source-mangler-",
        suffix=".txt",
        delete=False,
    ) as report:
        for problem in problems:
            report.write(check_source_problem_report_line(problem))
        return Path(report.name)


def check_source_problem_report_line(problem: CheckSourceProblem) -> str:
    suffix = " [deleted/]" if problem.kind == CHECK_SOURCE_DELETED_KIND else ""
    return f"{problem.path}{suffix}\n"


def open_check_source_missing_report(report_path: Path) -> None:
    command = ["notepad", str(report_path)] if sys.platform == "win32" else ["gvim", str(report_path)]
    try:
        subprocess.Popen(command)  # noqa: S603 - launches a local editor chosen by platform
    except OSError as exc:
        editor = command[0]
        print(f"Kunne ikke åpne {report_path} med {editor}: {exc}", file=sys.stderr)


def iter_check_source_files(root: Path):
    walk_errors: list[WalkError] = []

    def onerror(exc: OSError) -> None:
        path = Path(exc.filename) if exc.filename else root
        walk_errors.append(WalkError(path=path, message=str(exc)))

    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
        while walk_errors:
            yield walk_errors.pop(0)
        dirnames.sort()
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            try:
                if path.is_file():
                    yield path
            except OSError as exc:
                yield WalkError(path=path, message=str(exc))
    while walk_errors:
        yield walk_errors.pop(0)


def is_google_json_sidecar(path: Path) -> bool:
    if path.suffix.lower() != ".json":
        return False
    media_path = path.with_name(path.name[:-5])
    try:
        return media_path.is_file() and is_supported_media(media_path)
    except OSError:
        return False


def check_source_inventory_problems(
    initial_items: list[Path | WalkError],
    final_items: list[Path | WalkError],
) -> list[CheckSourceProblem]:
    problems: list[CheckSourceProblem] = []
    initial_paths = {
        item for item in initial_items if not isinstance(item, WalkError)
    }
    final_paths = {
        item for item in final_items if not isinstance(item, WalkError)
    }
    initial_errors = {
        (item.path, item.message)
        for item in initial_items
        if isinstance(item, WalkError)
    }

    for item in final_items:
        if (
            isinstance(item, WalkError)
            and (item.path, item.message) not in initial_errors
        ):
            problems.append(
                CheckSourceProblem(
                    item.path,
                    f"kan ikke lese kilden ved sluttkontrollen: {item.message}",
                )
            )
    for path in sorted(final_paths - initial_paths, key=str):
        problems.append(
            CheckSourceProblem(
                path,
                "filen kom til etter første filoversikt; kjør check-source på nytt",
            )
        )
    for path in sorted(initial_paths - final_paths, key=str):
        problems.append(
            CheckSourceProblem(
                path,
                "filen forsvant etter første filoversikt; kjør check-source på nytt",
            )
        )
    return problems


def check_source_hash_is_validated(target: Path, rows: list, target_hash_cache: dict[int, bool]) -> bool:
    valid = False
    for row in rows:
        file_id = int(row["id"])
        if file_id not in target_hash_cache:
            target_hash_cache[file_id] = validate_check_source_target_file(target, row)
        valid = valid or target_hash_cache[file_id]
    return valid


def validate_check_source_target_file(target: Path, row) -> bool:
    try:
        target_path = parse_collection_relative_path(row["target_path"])
    except InvalidCollectionRelativePath:
        return False

    if row["deleted_at"] is None:
        if not is_active_collection_file_path(target_path):
            return False
    elif not is_deleted_collection_file_path(target_path):
        return False

    try:
        actual_sha256, actual_size = hash_stable_collection_file(
            target,
            target_path,
        )
    except (CollectionFileHashError, OSError):
        return False
    return (
        actual_sha256 == row["sha256"]
        and actual_size == row["size_bytes"]
    )


def check_source_database_errors(
    target: Path,
    conn: sqlite3.Connection,
) -> list[str]:
    errors: list[str] = []
    integrity_errors: list[str] | None = None
    foreign_key_errors: list[sqlite3.Row] | None = None

    try:
        integrity_errors = db.database_integrity_errors(conn)
    except sqlite3.Error as exc:
        errors.append(f"SQLite integrity_check kunne ikke kjøres: {exc}")
    else:
        if integrity_errors:
            errors.append(
                "SQLite integrity_check fant "
                f"{len(integrity_errors)} feil: {integrity_errors[0]}"
            )

    try:
        foreign_key_errors = db.database_foreign_key_errors(conn)
    except sqlite3.Error as exc:
        errors.append(f"SQLite foreign_key_check kunne ikke kjøres: {exc}")
    else:
        if foreign_key_errors:
            first_foreign_key_error = foreign_key_errors[0]
            errors.append(
                "SQLite foreign_key_check fant "
                f"{len(foreign_key_errors)} ugyldig(e) referanse(r); "
                f"første: table={first_foreign_key_error['table']} "
                f"rowid={first_foreign_key_error['rowid']} "
                f"parent={first_foreign_key_error['parent']} "
                f"foreign_key={first_foreign_key_error['fkid']}"
            )

    if (
        integrity_errors != []
        or foreign_key_errors is None
        or foreign_key_errors
    ):
        return errors

    try:
        db.require_current_schema(conn)
    except (sqlite3.Error, ValueError) as exc:
        return [*errors, f"gjeldende databaseschema kunne ikke bekreftes: {exc}"]

    try:
        rows = db.file_path_integrity_rows(conn)
        path_issues = db.file_path_integrity_issues(conn)
    except sqlite3.Error as exc:
        return [
            *errors,
            f"databaseførte samlingsstier kunne ikke kontrolleres: {exc}",
        ]

    if path_issues:
        first_path_issue = path_issues[0]
        errors.append(
            f"{len(path_issues)} databaseført(e) stifeil; første: "
            f"file #{first_path_issue.file_id} "
            f"{first_path_issue.field}={first_path_issue.value!r}: "
            f"{first_path_issue.message}"
        )

    component_issues: list[tuple[int, str, object, Path, str]] = []
    for row in rows:
        for field in ("target_path", "deleted_original_target_path"):
            value = row[field]
            if value is None:
                continue
            try:
                relative_path = parse_collection_relative_path(value)
            except InvalidCollectionRelativePath:
                continue
            issue = inspect_existing_collection_path_components(
                target,
                relative_path,
            )
            if issue is not None:
                component_issues.append(
                    (
                        int(row["id"]),
                        field,
                        value,
                        issue.path,
                        issue.reason,
                    )
                )

    if component_issues:
        file_id, field, value, component, reason = component_issues[0]
        errors.append(
            f"{len(component_issues)} databaseført(e) sti(er) går gjennom "
            f"en usikker stikomponent; første: file #{file_id} "
            f"{field}={value!r}: {component} ({reason})"
        )
    return errors


def print_check_source_database_errors(
    source: Path,
    errors: list[str],
) -> None:
    print("Check-source")
    print(f"  Kildemappe: {source}")
    print(
        "  Hoveddatabasen eller databaseførte samlingsstier kunne ikke "
        "bekreftes som trygge."
    )
    print("  Kildemappen er derfor ikke trygg å slette.")
    print("Databaseproblemer:")
    for error in errors:
        print(f"- {error}")
    print("  Råd: Kjør bildebank doctor og undersøk sikkerhetskopien før du gjør endringer.")


def check_source_progress() -> ProgressMeter:
    global CHECK_SOURCE_PROGRESS
    CHECK_SOURCE_PROGRESS = ProgressMeter("Check-source", stream=sys.stderr)
    return CHECK_SOURCE_PROGRESS


def check_source_progress_details(stats: CheckSourceStats) -> str:
    return (
        f"dekket={stats.covered}, mangler={stats.missing}, slettet={stats.deleted}, "
        f"ignorert_json={stats.ignored_json}, kildefeil={stats.source_errors}, målfeil={stats.target_errors}"
    )


def check_source_is_safe(stats: CheckSourceStats, *, accept_deleted: bool = False) -> bool:
    return (
        stats.missing == 0
        and stats.source_errors == 0
        and stats.target_errors == 0
        and (accept_deleted or stats.deleted == 0)
    )


def print_check_source_report(
    source: Path,
    stats: CheckSourceStats,
    problems: list[CheckSourceProblem],
    *,
    problem_report_path: Path | None = None,
) -> None:
    print("Check-source")
    print(f"  Kildemappe: {source}")
    print(
        "  Oppsummering: "
        f"scannet={stats.scanned}, dekket={stats.covered}, mangler={stats.missing}, slettet={stats.deleted}, "
        f"ignorert_json={stats.ignored_json}, kildefeil={stats.source_errors}, målfeil={stats.target_errors}"
    )
    if stats.ignored_json:
        sidecar_label = (
            "Google JSON-sidecarfil ble"
            if stats.ignored_json == 1
            else "Google JSON-sidecarfiler ble"
        )
        print(
            f"  {stats.ignored_json} {sidecar_label} bevisst ignorert og "
            "er ikke kontrollert mot bildesamlingen."
        )
    if problems:
        print("  Det finnes filer som ikke er aktive i bildesamlingen, eller som ikke kan valideres.")
        print("  Kildemappen er derfor ikke trygg å slette.")
        print("Problemer:")
        for problem in problems:
            print(f"- {check_source_problem_report_line(problem).rstrip()}")
            print(f"  {problem.reason}")
        if problem_report_path is not None:
            print()
            print(f"Liste over problemfiler er lagret i: {problem_report_path}")
        return

    checked_label = "Alle kontrollerte filer" if stats.ignored_json else "Alle filer i kildemappen"
    if stats.deleted:
        print(f"  {checked_label} finnes i bildesamlingen eller deleted/ og er validert med SHA-256.")
    else:
        print(f"  {checked_label} finnes i bildesamlingen og er validert med SHA-256.")
    print("  Bildebank sletter ikke kildemapper.")
    print("  Hvis du vil slette mappen selv i PowerShell:")
    print()
    print(f"  Remove-Item -LiteralPath {powershell_literal(str(source))}")
    print()
    print("  Hvis mappen inneholder filer, spør PowerShell før den sletter.")


def powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
