from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from . import __version__, db
from .exiftool_probe import exiftool_metadata_gaps
from .importer import (
    import_pending_sources,
    import_pending_sources_dry_run,
    import_source,
    import_source_dry_run,
    refresh_non_metadata_files,
    validate_new_directory_source,
    validate_source_target,
)
from .html_export import export_html, export_html_conflicts
from .media import explain_date, image_dimensions, inspect_metadata
from .target_lock import TargetLock


HELP_COMMAND_GROUPS = (
    (
        "kom i gang",
        (
            ("create", "Opprett en ny bildesamlingsmappe"),
            ("add", "Registrer en vanlig kildemappe"),
            ("import", "Importer registrerte kilder"),
            ("import-removable", "Importer CD, USB og andre flyttbare medier"),
        ),
    ),
    (
        "se og kontrollere samlingen",
        (
            ("status", "Vis antall importerte bilder og videoer"),
            ("make-browser", "Lag index.html for nettleseren"),
            ("open-browser", "Åpne HTML-browseren i nettleseren"),
            ("list-sources", "Vis registrerte kilder"),
            ("show-source", "Vis hvor en importert fil kom fra"),
        ),
    ),
    (
        "finne ting som bør kontrolleres",
        (
            ("conflicts", "List filer med navnekollisjon"),
            ("show-conflict", "Vis detaljer om en navnekollisjon"),
            ("non-metadata", "List filer der datoen ikke kom fra metadata"),
            ("errors", "List registrerte feil"),
        ),
    ),
    (
        "rydde",
        (
            ("remove", "Flytt en importert fil til deleted/"),
            ("list-removed", "List filer som er flyttet til deleted/"),
        ),
    ),
    (
        "avansert kontroll",
        (
            ("explain-date", "Forklar hvilken dato Bildebank ville brukt"),
            ("inspect-metadata", "Vis metadatafragmenter og datokandidater"),
            ("refresh-metadata", "Sjekk filer uten metadata på nytt"),
            ("exiftool-metadata-gaps", "Finn metadata Bildebank ikke leser ennå"),
            ("make-conflict-browser", "Lag HTML-side for navnekollisjoner"),
            ("report", "Vis importoppsummering"),
        ),
    ),
    (
        "programmet",
        (
            ("migrate", "Oppgrader databasen etter programoppdatering"),
            ("update", "Oppdater programinstallasjonen"),
        ),
    ),
)


class BildebankHelpFormatter(argparse.RawDescriptionHelpFormatter):
    def _format_action(self, action: argparse.Action) -> str:
        if isinstance(action, argparse._SubParsersAction):
            return ""
        return super()._format_action(action)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        parser.print_help()
        return 0
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
    parser = argparse.ArgumentParser(
        prog="bildebank",
        usage="%(prog)s [-h] [--version] <kommando> [<args>]",
        formatter_class=BildebankHelpFormatter,
        epilog=main_help_epilog(),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--target", type=Path, help=argparse.SUPPRESS)

    subparsers = parser.add_subparsers(dest="command", required=True)

    create = add_command(
        subparsers,
        "create",
        usage="bildebank create [valg] mappe",
        help="Opprett målmappe og database",
    )
    create.add_argument("path", metavar="mappe", type=Path, help="Mappen som skal bli bildesamling")

    add = add_command(
        subparsers,
        "add",
        usage="bildebank add [valg] mappe",
        help="Registrer kildemappe",
    )
    add.add_argument("path", metavar="mappe", type=Path, help="Kildemappe med bilder og videoer")

    imp = add_command(
        subparsers,
        "import",
        usage="bildebank import [valg]",
        help="Importer registrerte kilder",
        description="Importer registrerte kilder som ikke er importert før. Hvis ingen nye kilder er lagt til siden sist import ble kjørt, blir resultatet 0 scannet.",
        )
    imp.add_argument("--quiet", action="store_true")
    imp.add_argument(
        "--dry-run",
        action="store_true",
        help="List filer som ville blitt importert uten å kopiere eller endre databasen",
    )
    imp.add_argument(
        "--log-file",
        type=Path,
        help="Skriv dry-run-listen til fil i stedet for stdout",
    )

    removable = add_command(
        subparsers,
        "import-removable",
        usage="bildebank import-removable [valg] --name navn mappe",
        help="Registrer og importer ett flyttbart medium direkte; ikke bruk add først",
        description=(
            "Registrerer og importerer ett flyttbart medium i samme kommando. "
            "Bruk denne uten å kjøre add først. --name er en stabil etikett for "
            "mediet, siden samme drive letter/path kan brukes av ulike medier."
        ),
    )
    removable.add_argument(
        "--name",
        required=True,
        help="Stabil etikett for mediet, for eksempel teksten på en CD eller USB-disk",
    )
    removable.add_argument(
        "--dry-run",
        action="store_true",
        help="List filer som ville blitt importert uten å registrere mediet eller endre databasen",
    )
    removable.add_argument("path", metavar="mappe", type=Path, help="Path til mediet slik det er montert nå")

    add_command(subparsers, "list-sources", usage="bildebank list-sources [valg]", help="List registrerte kilder")
    add_command(subparsers, "status", usage="bildebank status [valg]", help="Vis antall filer fordelt på type og datokilde")
    add_command(subparsers, "conflicts", usage="bildebank conflicts [valg]", help="List importerte filer med navnekollisjon")
    show_conflict = add_command(
        subparsers,
        "show-conflict",
        usage="bildebank show-conflict [valg] fil",
        help="Vis alle kildefiler i samme navnekollisjon som en målfil",
    )
    show_conflict.add_argument("path", metavar="fil", type=Path, help="Importert målfil")
    show_source = add_command(
        subparsers,
        "show-source",
        usage="bildebank show-source [valg] fil",
        help="Vis hvilken kilde en importert målfil kommer fra",
    )
    show_source.add_argument("path", metavar="fil", type=Path, help="Importert målfil")
    remove = add_command(
        subparsers,
        "remove",
        usage="bildebank remove [valg] fil",
        help="Flytt en importert målfil til deleted/ og marker den som slettet",
    )
    remove.add_argument("path", metavar="fil", type=Path, help="Importert målfil som skal fjernes")
    add_command(subparsers, "list-removed", usage="bildebank list-removed [valg]", help="List filer som er markert som slettet")
    non_metadata = add_command(
        subparsers,
        "non-metadata",
        usage="bildebank non-metadata [valg]",
        help="List filer der datoen ikke kom fra metadata",
    )
    non_metadata.add_argument(
        "--with-source",
        action="store_true",
        help="Vis kildefil i tillegg til målfil",
    )
    exiftool_gaps = add_command(
        subparsers,
        "exiftool-metadata-gaps",
        usage="bildebank exiftool-metadata-gaps [valg]",
        help="Finn filer der ExifTool ser metadata-dato som bildebank ikke leser",
    )
    exiftool_gaps.add_argument(
        "--exiftool",
        type=Path,
        help="Path til exiftool.exe. Standard er exiftool.exe i målmappen.",
    )
    explain = add_command(
        subparsers,
        "explain-date",
        usage="bildebank explain-date [valg] fil",
        help="Forklar hvilken dato programmet ville brukt for en fil",
    )
    explain.add_argument("path", metavar="fil", type=Path, help="Bilde- eller videofil")
    inspect = add_command(
        subparsers,
        "inspect-metadata",
        usage="bildebank inspect-metadata [valg] fil",
        help="Vis metadatafragmenter og datokandidater for en fil",
    )
    inspect.add_argument("path", metavar="fil", type=Path, help="Bilde- eller videofil")
    refresh = add_command(
        subparsers,
        "refresh-metadata",
        usage="bildebank refresh-metadata [valg]",
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
    errors = add_command(subparsers, "errors", usage="bildebank errors [valg]", help="List registrerte feil")
    errors.add_argument("--limit", type=int, default=50)
    errors.add_argument("--stage")
    errors.add_argument(
        "--include-resolved",
        action="store_true",
        help="Vis også feil som senere er løst",
    )
    browser = add_command(
        subparsers,
        "make-browser",
        usage="bildebank make-browser [valg]",
        help="Lag index.html for å bla i importerte bilder og videoer",
    )
    browser.add_argument(
        "-o",
        "--output",
        dest="output",
        type=Path,
        help="Skriv HTML-filen hit. Standard: index.html i målmappen.",
    )
    add_browser_filter_arguments(browser)

    open_browser = add_command(
        subparsers,
        "open-browser",
        usage="bildebank open-browser [valg]",
        help="Åpne HTML-browseren i standard nettleser",
        description=(
            "Åpner en eksisterende HTML-browser i standard nettleser. "
            "Standard er index.html i målmappen."
        ),
    )
    open_browser.add_argument(
        "-f",
        "--file",
        dest="file",
        type=Path,
        help="HTML-filen som skal åpnes. Standard: index.html i målmappen.",
    )

    export_conflicts = add_command(
        subparsers,
        "make-conflict-browser",
        usage="bildebank make-conflict-browser [valg]",
        help="Lag HTML-side for browsing av navnekollisjoner",
    )
    export_conflicts.add_argument(
        "-o",
        "--output",
        dest="output",
        type=Path,
        help="Skriv HTML-filen hit. Standard: name-conflicts.html i målmappen.",
    )
    add_command(subparsers, "report", usage="bildebank report [valg]", help="Vis importoppsummering")
    migrate = add_command(
        subparsers,
        "migrate",
        usage="bildebank migrate [valg]",
        help="Oppgrader databasen til gjeldende format",
        description="Validerer og oppgraderer databasen etter en programoppdatering.",
    )
    migrate.add_argument(
        "--check",
        action="store_true",
        help="Vis hva migreringen vil gjøre uten å endre databasen",
    )
    add_command(subparsers, "update", usage="bildebank update [valg]", help="Oppdater programinstallasjonen",
                description="Laster ned aller siste versjon av programmet fra GitHub.")

    return parser


def add_browser_filter_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-m",
        "--media",
        choices=("all", "image", "video"),
        default="all",
        help="Filtrer browseren til bilder, videoer eller begge deler.",
    )
    parser.add_argument(
        "--date-source",
        choices=("all", "metadata", "filename", "mtime", "unknown"),
        default="all",
        help="Filtrer browseren etter hvilken datokilde filene er plassert med.",
    )
    parser.add_argument(
        "--month-preview-limit",
        type=positive_int_arg,
        help="Maks antall filer i månedsoversikten. Standard: vis alle.",
    )


def add_command(
    subparsers: argparse._SubParsersAction,
    name: str,
    *,
    usage: str,
    help: str,
    description: str | None = None,
) -> argparse.ArgumentParser:
    return subparsers.add_parser(
        name,
        help=help,
        description=description,
        usage=usage,
    )


def main_help_epilog() -> str:
    lines = ["Vanlige kommandoer:"]
    for heading, commands in HELP_COMMAND_GROUPS:
        lines.append("")
        lines.append(heading)
        for command, description in commands:
            lines.append(f"   {command:<22} {description}")
    lines.append("")
    lines.append("Se hjelp for en kommando med:")
    lines.append("   bildebank <kommando> -h")
    lines.append("")
    lines.append("Eksempel:")
    lines.append("   bildebank import --dry-run")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    if args.command == "create":
        target = args.path.resolve()
        validate_target_not_in_program_repo(target)
        db.init_database(target)
        conn = db.connect(target)
        try:
            db.log_command(conn, "create", {"path": str(target)})
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

    if args.command == "update":
        return run_update()

    target = resolve_target(args.target)
    if args.command == "migrate":
        return run_migrate(target, check=args.check)

    if args.command == "import" and args.dry_run:
        output_path = args.log_file.resolve() if args.log_file else None
        if output_path is None:
            stats = import_pending_sources_dry_run(target, output=sys.stdout, verbose=not args.quiet)
        else:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with output_path.open("w", encoding="utf-8", newline="\n") as output:
                stats = import_pending_sources_dry_run(target, output=output, verbose=not args.quiet)
            print(f"Skrev dry-run importliste: {output_path}")
        print_summary(stats)
        return 0 if stats.errors == 0 else 2

    conn = db.connect(target)
    try:
        if not (args.command == "import-removable" and args.dry_run):
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
            if args.dry_run:
                source_row = db.Source(
                    id=0,
                    kind="removable",
                    path=source,
                    path_key=None,
                    name=args.name,
                    imported_at=None,
                    status="dry-run",
                    superseded_by_source_id=None,
                )
                stats = import_source_dry_run(
                    conn, target, source_row, output=sys.stdout, verbose=True
                )
                print_summary(stats)
                return 0 if stats.errors == 0 else 2
            with TargetLock(target, command="import-removable"):
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

        if args.command == "status":
            print_status(conn)
            return 0

        if args.command == "conflicts":
            for row in db.name_conflicts(conn):
                print(f"{row['source_path']} -> {row['target_path']}")
            return 0

        if args.command == "show-conflict":
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

        if args.command == "show-source":
            path = existing_path_arg(args.path).resolve()
            rows = db.file_sources_by_target_path(conn, path)
            if not rows:
                raise ValueError(f"Filen finnes ikke i importdatabasen: {path}")
            print_source_items(rows)
            return 0

        if args.command == "remove":
            original_path = resolve_target_file_arg(target, args.path)
            row = db.file_by_target_path(conn, original_path)
            if row is None:
                raise ValueError(f"Filen finnes ikke i importdatabasen: {original_path}")
            if row["deleted_at"] is not None:
                raise ValueError(f"Filen er allerede markert som slettet: {original_path}")
            if not original_path.exists():
                raise ValueError(f"Målfilen finnes ikke på disk: {original_path}")

            relative_path = relative_path_under_target(target, original_path)
            deleted_path = target / "deleted" / relative_path
            if deleted_path.exists():
                raise ValueError(f"Slettemål finnes allerede: {deleted_path}")

            deleted_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(original_path), str(deleted_path))
            db.mark_file_deleted(
                conn,
                file_id=int(row["id"]),
                deleted_path=deleted_path,
                original_target_path=original_path,
            )
            conn.commit()
            print(f"Flyttet til slettet mappe: {deleted_path}")
            return 0

        if args.command == "list-removed":
            for row in db.deleted_files(conn):
                print_deleted_item(row)
            return 0

        if args.command == "non-metadata":
            for row in db.non_metadata_files(conn):
                taken_date = row["taken_date"] or "-"
                if args.with_source:
                    print(
                        f"{row['date_source']}\t{taken_date}\t"
                        f"{row['target_path']}\t{row['source_path']}"
                    )
                else:
                    print(f"{row['date_source']}\t{taken_date}\t{row['target_path']}")
            return 0

        if args.command == "exiftool-metadata-gaps":
            exiftool_path = args.exiftool.resolve() if args.exiftool else None
            conn.commit()
            conn.close()
            gaps = exiftool_metadata_gaps(target, exiftool_path=exiftool_path, progress=True)
            for gap in gaps:
                print(
                    f"{gap.date}\t{gap.tag}\t{gap.value}\t"
                    f"bildebank={gap.bdb_source}:{gap.bdb_date}\t{gap.target_path}"
                )
            print(f"Oppsummering: exiftool_metadata_funnet={len(gaps)}")
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
                include_resolved=args.include_resolved,
            ):
                source_path = row["source_path"] or "-"
                resolved = row["resolved_at"] or "-"
                print(
                    f"{row['id']}\t{row['created_at']}\t{row['stage']}\t"
                    f"{resolved}\t{source_path}\t{row['message']}"
                )
            return 0

        if args.command == "make-browser":
            output = args.output.resolve() if args.output else None
            conn.commit()
            conn.close()
            output_path = export_html(
                target,
                output,
                media_filter=args.media,
                date_source_filter=args.date_source,
                month_preview_limit=args.month_preview_limit,
            )
            print(f"Skrev HTML-browser: {output_path}")
            return 0

        if args.command == "open-browser":
            conn.commit()
            conn.close()
            browser_path = resolve_browser_file_arg(target, args.file)
            if not browser_path.exists():
                raise ValueError(
                    f"Fant ikke HTML-browseren: {browser_path}. "
                    "Kjør bildebank make-browser først."
                )
            open_file_in_browser(browser_path)
            print(f"Åpnet HTML-browser: {browser_path}")
            return 0

        if args.command == "make-conflict-browser":
            output = args.output.resolve() if args.output else None
            conn.commit()
            conn.close()
            output_path = export_html_conflicts(target, output)
            print(f"Skrev HTML-browser for navnekollisjoner: {output_path}")
            return 0

        if args.command == "report":
            print_report(conn)
            return 0

        if args.command == "import":
            if args.log_file:
                raise ValueError("--log-file kan bare brukes sammen med --dry-run.")
            conn.commit()
            conn.close()
            with TargetLock(target, command="import"):
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
        raise ValueError("Fant ingen målmappe. Kjør kommandoen fra bildesamlingsmappen.")
    return target


def validate_target_not_in_program_repo(target: Path) -> None:
    repo_root = program_repo_root()
    try:
        target.resolve().relative_to(repo_root)
    except ValueError:
        return
    raise ValueError(
        "Målmappen kan ikke ligge inni programmappen. "
        "Velg en egen bildesamlingsmappe utenfor repoet."
    )


def program_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def run_update() -> int:
    if sys.platform == "win32":
        return run_update_windows()
    return run_update_linux()


def run_migrate(target: Path, *, check: bool) -> int:
    plan = db.migration_plan(target, validate=check)
    print(f"Målmappe: {target}")
    print(f"Database: {db.db_path_for_target(target)}")
    print(f"Nåværende schema_version: {plan.current_version}")
    print(f"Ny schema_version: {plan.target_version}")
    if plan.current_version == plan.target_version:
        print("Databasen er allerede migrert.")
        return 0

    if plan.creates_file_sources:
        print("Vil opprette tabellen file_sources.")
    else:
        print("Vil validere eksisterende file_sources-tabell.")
    print("Vil migrere:")
    print(f"  importerte filer: {plan.imported_files}")
    print(f"  duplikatfunn: {plan.duplicate_findings}")
    print("Vil lage backup før endring.")
    if check:
        print("Ingen endringer er gjort (--check).")
        return 0

    with TargetLock(target, command="migrate"):
        print("Låser målmappe.")
        backup_path = db.backup_database(target)
        print(f"Lager backup: {backup_path}")
        print("Validerer eksisterende database.")
        try:
            result = db.migrate_database(target)
        except Exception as exc:
            raise ValueError(
                f"{exc}\n"
                "Databasen ble ikke migrert. Ingen endringer er skrevet.\n"
                "Backup er beholdt for sikkerhet."
            ) from exc

    if result.creates_file_sources:
        print("Oppretter file_sources.")
    else:
        print("Validerer file_sources.")
    print(f"Migrerer importerte filer: {result.imported_files}")
    print(f"Migrerer duplikatfunn: {result.duplicate_findings}")
    print(f"Setter schema_version={result.target_version}.")
    print("Ferdig. Databasen er migrert.")
    return 0


def run_update_windows() -> int:
    repo_root = program_repo_root()
    update_script = repo_root / "update.ps1"
    if not update_script.exists():
        raise ValueError(
            f"Fant ikke update.ps1 i programmappen: {repo_root}. "
            f"Kjør manuelt fra programmappen hvis nødvendig."
        )
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(update_script),
            ],
            check=False,
        )
    except FileNotFoundError as exc:
        raise ValueError(
            "Fant ikke PowerShell. Kjør oppdatering manuelt fra programmappen: "
            f"cd {repo_root}; .\\update.ps1"
        ) from exc
    return completed.returncode


def run_update_linux() -> int:
    repo_root = program_repo_root()
    if not (repo_root / ".git").exists():
        raise ValueError(f"Fant ikke git-repo: {repo_root}")
    if not (repo_root / "pyproject.toml").exists():
        raise ValueError(f"Fant ikke pyproject.toml i: {repo_root}")

    run_update_command(["git", "pull", "--ff-only"], cwd=repo_root)

    venv_python = repo_root / ".venv" / "bin" / "python"
    if not venv_python.exists():
        python = shutil.which("python3.13") or shutil.which("python3")
        if python is None:
            raise ValueError("Fant ikke python3.13 eller python3 for å lage .venv.")
        run_update_command([python, "-m", "venv", ".venv"], cwd=repo_root)

    run_update_command([str(venv_python), "-m", "pip", "install", "-e", "."], cwd=repo_root)
    print("Ferdig. Databasen migreres ikke automatisk.")
    print("Kjør bildebank migrate i en bildesamling hvis programmet ber om det.")
    return 0


def run_update_command(command: list[str], *, cwd: Path) -> None:
    try:
        completed = subprocess.run(command, cwd=cwd, check=False)
    except FileNotFoundError as exc:
        raise ValueError(f"Fant ikke kommandoen: {command[0]}") from exc
    if completed.returncode != 0:
        raise ValueError(
            f"Kommando feilet med exit code {completed.returncode}: {' '.join(command)}"
        )


def open_file_in_browser(path: Path) -> None:
    if not webbrowser.open(path.resolve().as_uri()):
        raise ValueError(f"Klarte ikke åpne nettleseren for: {path}")


def resolve_browser_file_arg(target: Path, path: Path | None) -> Path:
    if path is None:
        return target / "index.html"
    if path.is_absolute():
        return path.resolve()
    return (target / path).resolve()


def resolve_target_file_arg(target: Path, path: Path) -> Path:
    candidate = existing_path_arg(path)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (target / candidate).resolve()
    relative_path_under_target(target, resolved)
    return resolved


def relative_path_under_target(target: Path, path: Path) -> Path:
    try:
        relative_path = path.resolve().relative_to(target.resolve())
    except ValueError as exc:
        raise ValueError(f"Filen ligger ikke i målmappen: {path}") from exc
    if not relative_path.parts or relative_path.parts[0] == "deleted":
        raise ValueError(f"Kan ikke slette filer fra deleted/: {path}")
    return relative_path


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


def print_source_item(row) -> None:
    source_path = Path(str(row["source_path"]))
    source_label = row["source_name"] or row["source_root"]
    print(f"Målfil: {row['target_path']}")
    print(f"Kildefil: {source_path}")
    print(f"Kildefil finnes: {'ja' if source_path.exists() else 'nei'}")
    print(f"Kilde-id: {row['source_id']}")
    print(f"Kildetype: {row['source_kind']}")
    print(f"Kilde: {source_label}")
    print(f"Kildestatus: {row['source_status']}")
    print(f"Originalt filnavn: {row['original_filename']}")
    print(f"Lagret filnavn: {row['stored_filename']}")
    print(f"Importert: {row['file_imported_at']}")
    print(f"Dato: {row['taken_date'] or '-'} ({row['date_source']})")
    print(f"Filstørrelse: {format_bytes(int(row['size_bytes']))} ({row['size_bytes']} bytes)")
    print(f"SHA-256: {row['sha256']}")


def print_source_items(rows: list) -> None:
    if len(rows) == 1:
        print_source_item(rows[0])
        return

    first = rows[0]
    print(f"Målfil: {first['target_path']}")
    print(f"Originalt filnavn: {first['original_filename']}")
    print(f"Lagret filnavn: {first['stored_filename']}")
    print(f"Importert: {first['file_imported_at']}")
    print(f"Dato: {first['taken_date'] or '-'} ({first['date_source']})")
    print(f"Filstørrelse: {format_bytes(int(first['size_bytes']))} ({first['size_bytes']} bytes)")
    print(f"SHA-256: {first['sha256']}")
    print("Kildefiler:")
    for row in rows:
        source_path = Path(str(row["source_path"]))
        source_label = row["source_name"] or row["source_root"]
        print(f"- {row['source_kind_label']}: {source_path}")
        print(f"  finnes: {'ja' if source_path.exists() else 'nei'}")
        print(f"  kilde-id: {row['source_id']}")
        print(f"  kildetype: {row['source_kind']}")
        print(f"  kilde: {source_label}")
        print(f"  kildestatus: {row['source_status']}")


def print_deleted_item(row) -> None:
    deleted_path = Path(str(row["target_path"]))
    original_path = row["deleted_original_target_path"] or "-"
    taken_date = row["taken_date"] or "-"
    exists = "ja" if deleted_path.exists() else "nei"
    print(f"{row['deleted_at']}\t{exists}\t{taken_date}\t{row['date_source']}\t{original_path}")
    print(f"  slettet fil: {deleted_path}")
    print(f"  kildefil: {row['source_path']}")
    print(f"  filstørrelse: {format_bytes(int(row['size_bytes']))} ({row['size_bytes']} bytes)")
    print(f"  sha256: {row['sha256']}")


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


def positive_int_arg(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("må være et heltall") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("må være minst 1")
    return number


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


def print_status(conn) -> None:
    counts = db.status_counts(conn)
    media = counts["media"]
    date_sources = counts["date_sources"]
    print(f"Totalt: {counts['total']}")
    print(f"Bilder: {media['bilder']}")
    print(f"Videoer: {media['videoer']}")
    print("Datokilde:")
    for source in ("metadata", "filename", "mtime", "unknown"):
        print(f"  {source}: {date_sources.get(source, 0)}")
    extra_sources = sorted(set(date_sources) - {"metadata", "filename", "mtime", "unknown"})
    for source in extra_sources:
        print(f"  {source}: {date_sources[source]}")


def print_report(conn) -> None:
    print(f"Kilder: {db.count_rows(conn, 'sources')}")
    print(f"Importerte filer: {db.count_rows(conn, 'files')}")
    print(f"Kildefilforekomster: {db.count_rows(conn, 'file_sources')}")
    print(f"Duplikatkilder: {db.duplicate_source_count(conn)}")
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
