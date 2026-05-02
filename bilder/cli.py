from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, db
from .importer import (
    import_pending_sources,
    import_source,
    refresh_non_metadata_files,
    validate_new_directory_source,
    validate_source_target,
)
from .html_export import export_html, export_html_conflicts
from .media import explain_date, image_dimensions, inspect_metadata


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return run(args)
    except KeyboardInterrupt:
        print("Avbrutt.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI should present readable errors
        print(f"Feil: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bdb")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--target", type=Path, help="Målmappe med .bilder.sqlite3")

    subparsers = parser.add_subparsers(dest="command", required=True)

    target = subparsers.add_parser("target", help="Opprett målmappe og database")
    target.add_argument("path", type=Path)

    add = subparsers.add_parser("add", help="Registrer kildemappe")
    add.add_argument("path", type=Path)

    imp = subparsers.add_parser("import", help="Importer registrerte kilder")
    imp.add_argument("--quiet", action="store_true")

    removable = subparsers.add_parser("import-removable", help="Importer flyttbart medium")
    removable.add_argument("--name", required=True)
    removable.add_argument("path", type=Path)

    subparsers.add_parser("list-sources", help="List registrerte kilder")
    subparsers.add_parser("list-name-conflicts", help="List importerte filer med navnekollisjon")
    show_name_conflict = subparsers.add_parser(
        "show-name-conflict",
        help="Vis alle kildefiler i samme navnekollisjon som en målfil",
    )
    show_name_conflict.add_argument("path", type=Path)
    non_metadata = subparsers.add_parser(
        "non-metadata",
        help="List filer der datoen ikke kom fra metadata",
    )
    non_metadata.add_argument(
        "--source",
        action="store_true",
        help="Vis kildefil i tillegg til målfil",
    )
    explain = subparsers.add_parser(
        "explain-date",
        help="Forklar hvilken dato programmet ville brukt for en fil",
    )
    explain.add_argument("path", type=Path)
    inspect = subparsers.add_parser(
        "inspect-metadata",
        help="Vis metadatafragmenter og datokandidater for en fil",
    )
    inspect.add_argument("path", type=Path)
    refresh = subparsers.add_parser(
        "refresh-metadata",
        help="Sjekk filer uten metadata på nytt og flytt dem hvis metadata nå kan leses",
    )
    refresh.add_argument(
        "--dry-run",
        action="store_true",
        help="Vis oppsummering uten å flytte filer eller endre databasen",
    )
    refresh.add_argument(
        "--verbose",
        action="store_true",
        help="Vis filer som flyttes, hoppes over eller feiler",
    )
    errors = subparsers.add_parser("errors", help="List registrerte feil")
    errors.add_argument("--limit", type=int, default=50)
    errors.add_argument("--stage")
    errors.add_argument(
        "--all",
        action="store_true",
        help="Vis også feil som senere er løst",
    )
    export = subparsers.add_parser(
        "export-html",
        help="Skriv index.html i målmappen for browsing av importerte filer",
    )
    export.add_argument("--output", type=Path)
    export_conflicts = subparsers.add_parser(
        "export-html-conflict",
        help="Skriv HTML-side for browsing av navnekollisjoner",
    )
    export_conflicts.add_argument("--output", type=Path)
    subparsers.add_parser("report", help="Vis importoppsummering")

    return parser


def run(args: argparse.Namespace) -> int:
    if args.command == "target":
        target = args.path.resolve()
        db.init_database(target)
        conn = db.connect(target)
        try:
            db.log_command(conn, "target", {"path": str(target)})
            conn.commit()
        finally:
            conn.close()
        print(f"Målmappe opprettet: {target}")
        return 0

    if args.command == "explain-date":
        path = existing_path_arg(args.path).resolve()
        if not path.exists():
            raise ValueError(f"Filen finnes ikke: {path}")
        explanation = explain_date(path)
        print(f"Fil: {path}")
        print(f"Støttet mediafil: {'ja' if explanation.supported_media else 'nei'}")
        selected_date = explanation.selected.date.isoformat() if explanation.selected.date else "-"
        print(f"Valgt dato: {selected_date}")
        print(f"Valgt kilde: {explanation.selected.source}")
        print("Kandidater:")
        for candidate in explanation.candidates:
            value = candidate.date.isoformat() if candidate.date else "-"
            print(f"  {candidate.source}\t{value}\t{candidate.detail}")
        return 0

    if args.command == "inspect-metadata":
        path = existing_path_arg(args.path).resolve()
        if not path.exists():
            raise ValueError(f"Filen finnes ikke: {path}")
        inspection = inspect_metadata(path)
        for line in inspection.lines:
            print(line)
        return 0

    target = resolve_target(args.target)
    conn = db.connect(target)
    try:
        db.log_command(conn, args.command, vars_for_log(args))
        if args.command == "add":
            source = existing_path_arg(args.path).resolve()
            if not source.is_dir():
                raise ValueError(f"Kildemappen finnes ikke: {source}")
            validate_source_target(source, target)
            validate_new_directory_source(conn, source)
            source_id = db.add_directory_source(conn, source)
            conn.commit()
            print(f"Registrert kildemappe #{source_id}: {source}")
            return 0

        if args.command == "import-removable":
            source = existing_path_arg(args.path).resolve()
            if not source.is_dir():
                raise ValueError(f"Mediet finnes ikke som mappe: {source}")
            validate_source_target(source, target)
            source_id = db.add_removable_source(conn, source, args.name)
            conn.commit()
            print(f"Registrert flyttbart medium #{source_id}: {args.name} ({source})")
            source_row = db.get_source(conn, source_id)
            if source_row.imported_at is not None:
                print(f"Flyttbart medium er allerede importert: {args.name}")
                return 0
            stats = import_source(conn, target, source_row, verbose=True)
            print_summary(stats)
            return 0 if stats.errors == 0 else 2

        if args.command == "list-sources":
            for source in db.get_sources(conn):
                label = source.name if source.name else source.path
                imported = source.imported_at or "-"
                superseded_by = source.superseded_by_source_id or "-"
                print(
                    f"{source.id}\t{source.kind}\t{source.status}\t"
                    f"{imported}\t{superseded_by}\t{label}"
                )
            return 0

        if args.command == "list-name-conflicts":
            for row in db.name_conflicts(conn):
                print(f"{row['source_path']} -> {row['target_path']}")
            return 0

        if args.command == "show-name-conflict":
            path = existing_path_arg(args.path).resolve()
            row = db.file_by_target_path(conn, path)
            if row is None:
                raise ValueError(f"Filen finnes ikke i importdatabasen: {path}")
            rows = name_conflict_group(conn, row)
            if len(rows) < 2 or not any(item["name_conflict"] for item in rows):
                print(f"Filen er ikke del av en navnekollisjon: {path}")
                return 0
            print(f"Navnekollisjon: {row['original_filename']}")
            print(f"Målmappe: {Path(str(row['target_path'])).parent}")
            for item in rows:
                print_name_conflict_item(item)
            return 0

        if args.command == "non-metadata":
            for row in db.non_metadata_files(conn):
                taken_date = row["taken_date"] or "-"
                if args.source:
                    print(
                        f"{row['date_source']}\t{taken_date}\t"
                        f"{row['target_path']}\t{row['source_path']}"
                    )
                else:
                    print(f"{row['date_source']}\t{taken_date}\t{row['target_path']}")
            return 0

        if args.command == "refresh-metadata":
            conn.commit()
            conn.close()
            stats = refresh_non_metadata_files(
                target, dry_run=args.dry_run, verbose=args.verbose
            )
            print_refresh_summary(stats, dry_run=args.dry_run)
            return 0 if stats.errors == 0 else 2

        if args.command == "errors":
            for row in db.errors(
                conn,
                limit=args.limit,
                stage=args.stage,
                include_resolved=args.all,
            ):
                source_path = row["source_path"] or "-"
                resolved = row["resolved_at"] or "-"
                print(
                    f"{row['id']}\t{row['created_at']}\t{row['stage']}\t"
                    f"{resolved}\t{source_path}\t{row['message']}"
                )
            return 0

        if args.command == "export-html":
            output = args.output.resolve() if args.output else None
            output_path = export_html(target, output)
            print(f"Skrev HTML-browser: {output_path}")
            return 0

        if args.command == "export-html-conflict":
            output = args.output.resolve() if args.output else None
            output_path = export_html_conflicts(target, output)
            print(f"Skrev HTML-browser for navnekollisjoner: {output_path}")
            return 0

        if args.command == "report":
            print_report(conn)
            return 0

        if args.command == "import":
            conn.commit()
            conn.close()
            stats = import_pending_sources(target, verbose=not args.quiet)
            print_summary(stats)
            return 0 if stats.errors == 0 else 2

        raise ValueError(f"Ukjent kommando: {args.command}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def resolve_target(target_arg: Path | None) -> Path:
    if target_arg is not None:
        target = target_arg.resolve()
        if not db.db_path_for_target(target).exists():
            raise ValueError(f"Målmappen er ikke initialisert: {target}")
        return target
    target = db.find_target()
    if target is None:
        raise ValueError("Fant ingen målmappe. Kjør fra målmappen eller bruk --target.")
    return target


def name_conflict_group(conn, row) -> list:
    parent_key = db.path_key(Path(str(row["target_path"])).parent)
    rows = []
    for candidate in db.files_by_original_filename(conn, str(row["original_filename"])):
        if db.path_key(Path(str(candidate["target_path"])).parent) == parent_key:
            rows.append(candidate)
    return rows


def print_name_conflict_item(row) -> None:
    target_path = Path(str(row["target_path"]))
    source_path = Path(str(row["source_path"]))
    dimensions = image_dimensions(target_path)
    dimensions_text = f"{dimensions.width}x{dimensions.height}" if dimensions else "-"
    taken_date = row["taken_date"] or "-"

    print(f"{row['stored_filename']}")
    print(f"  mål: {target_path}")
    print(f"  kilde: {source_path}")
    print(f"  kilde-id: {row['source_id']}")
    print(f"  dato: {taken_date} ({row['date_source']})")
    print(f"  oppløsning: {dimensions_text}")
    print(f"  filstørrelse: {format_bytes(int(row['size_bytes']))} ({row['size_bytes']} bytes)")
    print(f"  sha256: {row['sha256']}")
    print(f"  kildefil finnes: {'ja' if source_path.exists() else 'nei'}")


def format_bytes(size: int) -> str:
    units = ("bytes", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "bytes":
                return f"{size} bytes"
            return f"{value:.1f} {unit}"
        value /= 1024


def existing_path_arg(path: Path) -> Path:
    if path.exists():
        return path
    raw = str(path)
    stripped = raw.rstrip("\"'")
    if stripped != raw:
        candidate = Path(stripped)
        if candidate.exists():
            return candidate
    return path


def vars_for_log(args: argparse.Namespace) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in vars(args).items():
        if isinstance(value, Path):
            result[key] = str(value)
        else:
            result[key] = str(value)
    return result


def print_summary(stats) -> None:
    print(
        "Oppsummering: "
        f"scannet={stats.scanned}, importert={stats.imported}, "
        f"duplikater={stats.duplicates}, eksisterende={stats.skipped_existing}, "
        f"dekket={stats.skipped_covered}, navnekollisjoner={stats.name_conflicts}, "
        f"feil={stats.errors}"
    )


def print_report(conn) -> None:
    print(f"Kilder: {db.count_rows(conn, 'sources')}")
    print(f"Importerte filer: {db.count_rows(conn, 'files')}")
    print(f"Duplikatfunn: {db.count_rows(conn, 'duplicate_findings')}")
    print(f"Uløste feil: {db.error_count(conn)}")
    name_conflicts = conn.execute("SELECT COUNT(*) FROM files WHERE name_conflict = 1").fetchone()[0]
    undated = conn.execute("SELECT COUNT(*) FROM files WHERE date_source = 'unknown'").fetchone()[0]
    print(f"Navnekollisjoner: {name_conflicts}")
    print(f"Filer uten dato: {undated}")


def print_refresh_summary(stats, *, dry_run: bool) -> None:
    prefix = "Dry-run: " if dry_run else ""
    print(
        prefix
        + "Oppsummering: "
        f"sjekket={stats.checked}, metadata_funnet={stats.metadata_found}, "
        f"flyttet={stats.moved}, allerede_riktig={stats.already_correct}, feil={stats.errors}"
    )
