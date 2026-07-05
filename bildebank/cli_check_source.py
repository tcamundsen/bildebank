from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import db
from .importer import WalkError, validate_source_target
from .media import is_supported_media, sha256_file
from .progress import ProgressMeter


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

    conn = db.connect(target)
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
                file_hash = sha256_file(path)
            except OSError as exc:
                stats.source_errors += 1
                problems.append(CheckSourceProblem(path, f"kan ikke lese filen i kilden: {exc}"))
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


def check_source_hash_is_validated(target: Path, rows: list, target_hash_cache: dict[int, bool]) -> bool:
    valid = False
    for row in rows:
        file_id = int(row["id"])
        if file_id not in target_hash_cache:
            target_hash_cache[file_id] = validate_check_source_target_file(target, row)
        valid = valid or target_hash_cache[file_id]
    return valid


def validate_check_source_target_file(target: Path, row) -> bool:
    target_path = db.absolute_target_path(target, Path(str(row["target_path"])))
    if not target_path.exists() or not target_path.is_file():
        return False
    try:
        return sha256_file(target_path) == row["sha256"]
    except OSError:
        return False


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

    if stats.deleted:
        print("  Alle filer i kildemappen finnes i bildesamlingen eller deleted/ og er validert med SHA-256.")
    else:
        print("  Alle filer i kildemappen finnes i bildesamlingen og er validert med SHA-256.")
    print("  Bildebank sletter ikke kildemapper.")
    print("  Hvis du vil slette mappen selv i PowerShell:")
    print()
    print(f"  Remove-Item -LiteralPath {powershell_literal(str(source))}")
    print()
    print("  Hvis mappen inneholder filer, spør PowerShell før den sletter.")


def powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
