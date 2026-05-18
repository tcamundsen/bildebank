from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import sys
import time
import traceback
import webbrowser
from pathlib import Path

from . import __version__, db
from .backup import run_backup
from .config import CONFIG_FILENAME, load_config
from .exiftool_probe import exiftool_metadata_gaps
from .face import (
    AddFaceToPersonResult,
    DeletePersonResult,
    FaceResetResult,
    FaceReport,
    FaceSuggestStats,
    RemoveFaceFromPersonResult,
    add_face_to_person,
    create_person,
    delete_person,
    export_face_browser,
    export_people_browser,
    export_person_browser,
    face_db_path,
    face_db_summary,
    face_report,
    list_face_suggestions,
    list_persons,
    remove_face_from_person,
    reset_face_database,
    scan_faces,
    suggest_faces,
)
from .geo import (
    DEFAULT_EXIFTOOL_BATCH_SIZE,
    h3_column_for_resolution,
    h3_resolution,
    scan_geo,
)
from .importer import (
    import_source,
    import_source_dry_run,
    refresh_non_metadata_files,
    validate_source_target,
)
from .html_export import export_html, export_html_conflicts
from .media import explain_date, inspect_metadata, sha256_file
from .media_cache import MediaMetadataCache, cached_image_dimensions
from .openclip import (
    openclip_db_path,
    openclip_db_summary,
    scan_images,
    search_images,
    torch_gpu_status,
)
from .program_state import known_targets, program_db_path, record_target_best_effort
from .server import DEFAULT_HOST, DEFAULT_PORT, run_server as run_local_server
from .target_lock import TargetLock
from .thumbnails import ThumbnailStats, run_make_thumbnails


FACE_SCAN_PROGRESS_STARTED_AT: float | None = None
IMAGE_SCAN_PROGRESS_STARTED_AT: float | None = None


HELP_COMMAND_GROUPS = (
    (
        "kom i gang",
        (
            ("create", "Opprett en ny bildesamlingsmappe"),
            ("import", "Importer en mappe, CD, USB eller annen kilde"),
        ),
    ),
    (
        "se og kontrollere samlingen",
        (
            ("status", "Vis antall importerte bilder og videoer"),
            ("make-thumbnails", "Lag thumbnails for rask månedsvisning"),
            ("make-browser", "Lag index.html for nettleseren"),
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
            ("undelete", "Flytt en slettet fil tilbake fra deleted/"),
            ("unimport", "Reverser en tidligere importert kilde"),
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
            ("geo-scan", "Scan GPS-koordinater fra metadata"),
            ("geo-stats", "Vis GPS-status for bildesamlingen"),
            ("geo-areas", "List H3-områder med bilder"),
            ("geo-area", "List bilder i ett H3-område"),
            ("make-conflict-browser", "Lag HTML-side for navnekollisjoner"),
            ("report", "Vis importoppsummering"),
        ),
    ),
    (
        "programmet",
        (
            ("where-is", "Vis hvor Bildebank og kjente bildesamlinger ligger"),
            ("backup", "Lag eller oppdater backup av bildesamlingen"),
            ("face-status", "Vis status for valgfri ansiktsgjenkjenning"),
            ("image-scan", "Scan bilder for tekstbasert bildesøk"),
            ("image-search", "Søk etter bilder med tekst"),
            ("run-server", "Start lokal Bildebank-server"),
            ("face-scan", "Scanning etter ansikter"),
            ("face-report", "Vis rapport for scannede ansikter"),
            ("face-person-create", "Opprett person i ansiktsdatabasen"),
            ("face-person-add-face", "Koble ett ansikt til person"),
            ("face-person-remove-face", "Fjern ett ansikt fra person"),
            ("face-person-delete", "Slett person fra ansiktsdatabasen"),
            ("face-person-list", "List personer i ansiktsdatabasen"),
            ("face-suggest", "Foreslå personer for ukjente ansikter"),
            ("make-face-browser", "Lag HTML-side for scannede ansikter"),
            ("make-people-browser", "Lag HTML-sider for alle personer"),
            ("make-person-browser", "Lag HTML-side for en person"),
            ("face-reset", "Slett ansiktsdata"),
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
        if args.debug:
            traceback.print_exception(type(exc), exc, exc.__traceback__)
        else:
            print(f"Feil: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bildebank",
        usage="%(prog)s [-h] [--version] <kommando> [<args>]",
        description=f"Bildebank {__version__}",
        formatter_class=BildebankHelpFormatter,
        epilog=main_help_epilog(),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--debug", action="store_true", help="Vis traceback ved feil. Må settes først")
    parser.add_argument("--target", type=Path, help=argparse.SUPPRESS)

    subparsers = parser.add_subparsers(dest="command", required=True)

    create = add_command(
        subparsers,
        "create",
        usage="bildebank create [valg] mappe",
        help="Opprett bildesamling og database",
    )
    create.add_argument("path", metavar="mappe", type=Path, help="Mappen som skal bli bildesamling")

    imp = add_command(
        subparsers,
        "import",
        usage="bildebank import [valg] --name navn mappe",
        help="Importer en navngitt kilde direkte",
        description=(
            "Registrerer og importerer bildene fra en mappe, USB-brikke, CD eller disk."
        ),
        )
    imp.add_argument("--quiet", action="store_true")
    imp.add_argument(
        "--name",
        required=True,
        help="Unikt navn på importen, for eksempel Sommer2023 eller Familie-CD-2004",
    )
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
    imp.add_argument("path", metavar="mappe", type=Path, help="Kilden som skal importeres")

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
    unimport = add_command(
        subparsers,
        "unimport",
        usage="bildebank unimport [valg] --name navn",
        help="Reverser en tidligere importert kilde",
        description=(
            "Kontrollerer først at alle registrerte kildefiler fortsatt finnes "
            "med samme innhold. Krever nøyaktig bekreftelse før noe endres."
        ),
    )
    unimport.add_argument("--name", required=True, help="Navn på importen som skal reverseres")
    unimport.add_argument(
        "--dry-run",
        action="store_true",
        help="Vis hva som ville blitt gjort uten å slette filer eller endre databasen",
    )
    remove = add_command(
        subparsers,
        "remove",
        usage="bildebank remove [valg] fil",
        help="Flytt en importert målfil til deleted/ og marker den som slettet",
    )
    remove.add_argument("path", metavar="fil", type=Path, help="Importert målfil som skal fjernes")
    undelete = add_command(
        subparsers,
        "undelete",
        usage="bildebank undelete [valg] fil",
        help="Flytt en slettet fil tilbake fra deleted/",
    )
    undelete.add_argument("path", metavar="fil", type=Path, help="Slettet fil under deleted/")
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
        help="Path til exiftool.exe. Standard er exiftool.exe i bildesamlingsmappen.",
    )
    geo_scan = add_command(
        subparsers,
        "geo-scan",
        usage="bildebank geo-scan [valg]",
        help="Scan GPS-koordinater fra metadata",
    )
    geo_scan.add_argument("--force", action="store_true", help="Les GPS-metadata på nytt for filer som allerede er scannet")
    geo_scan.add_argument(
        "--only-missing",
        action="store_true",
        help="Scan bare filer uten GPS-data og uten tidligere GPS-resultat",
    )
    geo_scan.add_argument("--limit", type=positive_int_arg, help="Maks antall filer som skal scannes")
    geo_scan.add_argument("--verbose", action="store_true", help="Vis filer uten GPS eller med feil")
    geo_scan.add_argument(
        "--exiftool",
        type=Path,
        help="Path til exiftool. Standard er exiftool.exe i bildesamlingsmappen, ellers exiftool fra PATH.",
    )
    geo_scan.add_argument(
        "--batch-size",
        type=positive_int_arg,
        default=DEFAULT_EXIFTOOL_BATCH_SIZE,
        help=f"Antall filer per ExifTool-kall. Standard: {DEFAULT_EXIFTOOL_BATCH_SIZE}",
    )
    add_command(
        subparsers,
        "geo-stats",
        usage="bildebank geo-stats [valg]",
        help="Vis GPS-status for bildesamlingen",
    )
    geo_areas = add_command(
        subparsers,
        "geo-areas",
        usage="bildebank geo-areas [valg]",
        help="List H3-områder med bilder",
    )
    geo_areas.add_argument("--resolution", type=h3_resolution_arg, default=7, help="H3-oppløsning 5-9. Standard: 7")
    geo_areas.add_argument("--min-count", type=positive_int_arg, default=2, help="Vis områder med minst N bilder. Standard: 2")
    geo_areas.add_argument("--limit", type=positive_int_arg, default=50, help="Maks antall områder. Standard: 50")
    geo_area = add_command(
        subparsers,
        "geo-area",
        usage="bildebank geo-area [valg] h3_celle",
        help="List bilder i ett H3-område",
    )
    geo_area.add_argument("h3_cell", metavar="h3_celle", help="H3-celle fra geo-areas")
    geo_area.add_argument("--limit", type=positive_int_arg, help="Maks antall bilder som vises")
    geo_area.add_argument("--with-date", action="store_true", help="Vis taken_date")
    geo_area.add_argument("--with-coordinates", action="store_true", help="Vis GPS-koordinater")
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
        help="Skriv HTML-filen hit. Standard: index.html i bildesamlingsmappen.",
    )
    add_month_preview_limit_argument(browser)

    make_thumbnails = add_command(
        subparsers,
        "make-thumbnails",
        usage="bildebank make-thumbnails [valg]",
        help="Lag thumbnails for månedsvisning",
    )
    make_thumbnails.add_argument("--limit", type=positive_int_arg, help="Maks antall bildefiler som skal sjekkes")
    make_thumbnails.add_argument("--verbose", action="store_true", help="Vis filer som feiler")

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
        help="Skriv HTML-filen hit. Standard: name-conflicts.html i bildesamlingsmappen.",
    )
    add_command(subparsers, "report", usage="bildebank report [valg]", help="Vis importoppsummering")
    add_command(
        subparsers,
        "where-is",
        usage="bildebank where-is [valg]",
        help="Vis hvor Bildebank og kjente bildesamlinger ligger",
    )
    backup = add_command(
        subparsers,
        "backup",
        usage="bildebank backup [valg] plassering",
        help="Lag eller oppdater backup av bildesamlingen",
        description=(
            "Tolker plassering som foreldremappen der backupen skal ligge. "
            "Backupmappen får alltid samme navn som aktiv bildesamling."
        ),
    )
    backup.add_argument("destination", metavar="plassering", type=Path, help="Eksisterende mappe der backupen skal ligge")
    backup.add_argument(
        "--dry-run",
        action="store_true",
        help="Vis hva som ville blitt gjort uten å kopiere eller endre filer",
    )
    add_command(
        subparsers,
        "face-status",
        usage="bildebank face-status [valg]",
        help="Vis status for valgfri ansiktsgjenkjenning",
    )
    image_scan = add_command(
        subparsers,
        "image-scan",
        usage="bildebank image-scan [valg]",
        help="Scan bilder for tekstbasert bildesøk",
    )
    image_scan.add_argument("--limit", type=positive_int_arg, help="Maks antall bildefiler som skal scannes")
    image_search = add_command(
        subparsers,
        "image-search",
        usage="bildebank image-search [valg] søk",
        help="Søk etter bilder med tekst",
    )
    image_search.add_argument("query", metavar="søk", help="Søketekst, for eksempel strand")
    image_search.add_argument(
        "--limit",
        type=positive_int_arg,
        default=100,
        help="Maks antall treff. Standard: 100",
    )
    image_search.add_argument(
        "--no-browser",
        action="store_true",
        help="Ikke åpne image-search.html automatisk etter søket.",
    )
    run_server_parser = add_command(
        subparsers,
        "run-server",
        usage="bildebank run-server [valg]",
        help="Start lokal Bildebank-server",
    )
    run_server_parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"Adresse serveren lytter på. Standard: {DEFAULT_HOST}",
    )
    run_server_parser.add_argument(
        "--port",
        type=positive_int_arg,
        default=DEFAULT_PORT,
        help=f"Port serveren lytter på. Standard: {DEFAULT_PORT}",
    )
    run_server_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Ikke åpne serveren automatisk i nettleser.",
    )
    face_scan = add_command(
        subparsers,
        "face-scan",
        description="Scanner importerte bilder etter ansikter.",
        usage="bildebank face-scan [valg]",
        help="Scanning etter ansikter",
    )
    face_scan.add_argument("--limit", type=positive_int_arg, help="Maks antall bildefiler som skal sjekkes")
    face_scan.add_argument(
        "--show-model-output",
        action="store_true",
        help="Vis intern output fra InsightFace/ONNX ved feilsøking",
    )
    face_report_parser = add_command(
        subparsers,
        "face-report",
        usage="bildebank face-report [valg]",
        help="Vis rapport for scannede ansikter",
    )
    face_report_parser.add_argument("--limit", type=positive_int_arg, default=20, help="Maks antall linjer per liste")
    face_person_create = add_command(
        subparsers,
        "face-person-create",
        usage="bildebank face-person-create [valg] navn",
        help="Opprett person i ansiktsdatabasen",
    )
    face_person_create.add_argument("name", metavar="navn", help="Personnavn")
    face_person_add_face = add_command(
        subparsers,
        "face-person-add-face",
        usage="bildebank face-person-add-face [valg] navn ansikt_id",
        help="Koble ett ansikt til person",
    )
    face_person_add_face.add_argument("name", metavar="navn", help="Personnavn")
    face_person_add_face.add_argument("face_id", metavar="ansikt_id", type=positive_int_arg, help="Ansikt-id fra HTML-visningen")
    face_person_remove_face = add_command(
        subparsers,
        "face-person-remove-face",
        usage="bildebank face-person-remove-face [valg] navn ansikt_id",
        help="Fjern ett ansikt fra person",
    )
    face_person_remove_face.add_argument("name", metavar="navn", help="Personnavn")
    face_person_remove_face.add_argument("face_id", metavar="ansikt_id", type=positive_int_arg, help="Ansikt-id fra HTML-visningen")
    face_person_delete = add_command(
        subparsers,
        "face-person-delete",
        usage="bildebank face-person-delete [valg] navn",
        help="Slett person fra ansiktsdatabasen",
    )
    face_person_delete.add_argument("name", metavar="navn", help="Personnavn")
    add_command(
        subparsers,
        "face-person-list",
        usage="bildebank face-person-list [valg]",
        help="List personer i ansiktsdatabasen",
    )
    face_suggest = add_command(
        subparsers,
        "face-suggest",
        description="Foreslå personer for ukjente ansikter.",
        usage="bildebank face-suggest [valg]",
        help="Foreslå personer for ukjente ansikter",
    )
    face_suggest.add_argument(
        "--threshold",
        type=similarity_threshold_arg,
        default=0.6,
        help="Likhetsterskel fra 0.0 til 1.0. Standard: 0.6",
    )
    face_browser = add_command(
        subparsers,
        "make-face-browser",
        usage="bildebank make-face-browser [valg]",
        help="Debug: lag HTML-side for scannede ansikter. Ikke ment for vanlig bruk.",
        description=(
            "Debug-verktøy. Denne kommandoen lager faces.html for kontroll av "
            "scannede ansikter, men er ikke ment for vanlig bruk."
        ),
    )
    face_browser.add_argument(
        "--limit",
        type=positive_int_arg,
        help="Maks antall bilder som tas med. Anbefales fordi siden kan bli svært stor.",
    )
    face_browser.add_argument(
        "-o",
        "--output",
        dest="output",
        type=Path,
        help="Skriv HTML-filen hit. Standard: faces.html i bildesamlingen.",
    )
    person_browser = add_command(
        subparsers,
        "make-person-browser",
        usage="bildebank make-person-browser [valg] navn",
        help="Lag HTML-side for en person",
    )
    person_browser.add_argument("name", metavar="navn", help="Personnavn")
    person_browser.add_argument(
        "-o",
        "--output",
        dest="output",
        type=Path,
        help="Skriv HTML-filen hit. Standard: person-Navn.html i bildesamlingen.",
    )
    person_browser.add_argument(
        "--month-preview-limit",
        type=positive_int_arg,
        help="Maks antall bilder i månedsoversikten. Standard: vis alle.",
    )
    people_browser = add_command(
        subparsers,
        "make-people-browser",
        usage="bildebank make-people-browser [valg]",
        help="Lag HTML-index og personside for alle registrerte personer",
    )
    people_browser.add_argument(
        "--month-preview-limit",
        type=positive_int_arg,
        help="Maks antall bilder i månedsoversikten på hver personside. Standard: vis alle.",
    )
    face_reset = add_command(
        subparsers,
        "face-reset",
        usage="bildebank face-reset [valg]",
        help="Slett ansiktsdata",
        description=(
            "Sletter ansiktsdata på valgt nivå. Kommandoen krever alltid "
            "nøyaktig bekreftelse før noe slettes."
        ),
    )
    reset_mode = face_reset.add_mutually_exclusive_group()
    reset_mode.add_argument(
        "--all",
        action="store_true",
        help="Slett hele face-databasen, inkludert face-scan, personer og forslag.",
    )
    reset_mode.add_argument(
        "--keep-scan",
        action="store_true",
        help="Behold face-scan-resultater, men slett personer, bekreftelser og forslag. Standard hvis ingen nivåvalg er brukt.",
    )
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


def add_month_preview_limit_argument(parser: argparse.ArgumentParser) -> None:
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
    lines.append("   bildebank import --name \"USB-2024\" --dry-run E:\\")
    lines.append("   bildebank import --name \"Sommer2023\" \"C:\\Bilder\\Sommer2023\"")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    if args.command == "create":
        target = args.path.resolve()
        validate_target_not_in_program_repo(target)
        if db.db_path_for_target(target).exists():
            raise ValueError(f"Bildesamling finnes allerede: {target}")
        db.init_database(target)
        conn = db.connect(target)
        try:
            db.log_command(conn, "create", {"path": str(target)})
            conn.commit()
        finally:
            conn.close()
        record_target_best_effort(program_repo_root(), target, created=True)
        print(f"Bildesamling opprettet: {target}")
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

    if args.command == "where-is":
        return run_where_is()

    if args.command == "face-status":
        return run_face_status(args.target)

    target = resolve_target(args.target)
    record_target_best_effort(program_repo_root(), target)
    if args.command == "migrate":
        return run_migrate(target, check=args.check)

    if args.command == "backup":
        return run_backup_command(target, args.destination, dry_run=args.dry_run)

    if args.command == "geo-scan":
        return run_geo_scan(
            target,
            force=args.force,
            only_missing=args.only_missing,
            limit=args.limit,
            verbose=args.verbose,
            exiftool_path=args.exiftool,
            batch_size=args.batch_size,
        )

    if args.command == "geo-stats":
        return run_geo_stats(target)

    if args.command == "geo-areas":
        return run_geo_areas(target, resolution=args.resolution, min_count=args.min_count, limit=args.limit)

    if args.command == "geo-area":
        return run_geo_area(
            target,
            h3_cell=args.h3_cell,
            limit=args.limit,
            with_date=args.with_date,
            with_coordinates=args.with_coordinates,
        )

    if args.command in {"image-scan", "image-search"}:
        require_openclip_enabled(load_config(program_repo_root()).openclip.enabled)
        if args.command == "image-scan":
            return run_image_scan(target, limit=args.limit)
        return run_image_search(target, query=args.query, limit=args.limit, browsers=args.gen_browsers)

    if args.command == "run-server":
        return run_server_command(target, host=args.host, port=args.port, browser=not args.no_browser)

    face_commands = {
        "face-scan",
        "face-report",
        "face-person-create",
        "face-person-add-face",
        "face-person-remove-face",
        "face-person-delete",
        "face-person-list",
        "face-suggest",
        "make-face-browser",
        "make-person-browser",
        "make-people-browser",
        "face-reset",
    }
    if args.command in face_commands:
        require_face_enabled(load_config(program_repo_root()).face_recognition.enabled)

    if args.command == "face-scan":
        return run_face_scan(target, limit=args.limit, show_model_output=args.show_model_output)

    if args.command == "face-report":
        return run_face_report(target, limit=args.limit)

    if args.command == "face-person-create":
        person_id = create_person(target, args.name)
        print(f"Person #{person_id}: {args.name.strip()}")
        return 0

    if args.command == "face-person-add-face":
        result = add_face_to_person(target, args.name, args.face_id)
        print_add_face_to_person_result(result)
        return 0

    if args.command == "face-person-remove-face":
        result = remove_face_from_person(target, args.name, args.face_id)
        print_remove_face_from_person_result(result)
        return 0

    if args.command == "face-person-delete":
        return run_face_person_delete(target, args.name)

    if args.command == "face-person-list":
        print_persons(target)
        return 0

    if args.command == "face-suggest":
        return run_face_suggest(target, threshold=args.threshold)

    if args.command == "make-face-browser":
        output = args.output.resolve() if args.output else None
        output_path = export_face_browser(target, output, limit=args.limit)
        print(f"Skrev HTML-browser for ansikter: {output_path}")
        return 0

    if args.command == "make-person-browser":
        output = args.output.resolve() if args.output else None
        output_path = export_person_browser(
            target,
            args.name,
            output,
            month_preview_limit=args.month_preview_limit,
        )
        print(f"Skrev HTML-browser for person: {output_path}")
        return 0

    if args.command == "make-people-browser":
        result = export_people_browser(target, month_preview_limit=args.month_preview_limit)
        print(f"Skrev person-index: {result.index_path}")
        print(f"Skrev personsider: {len(result.person_pages)}")
        return 0

    if args.command == "face-reset":
        return run_face_reset(
            target,
            all_data=args.all,
        )

    if args.command == "import" and args.dry_run:
        return run_named_import_dry_run(target, args)

    conn = db.connect(target)
    try:
        if not (args.command == "unimport" and args.dry_run):
            db.log_command(conn, args.command, vars_for_log(args))
        if args.command == "import":
            if args.log_file:
                raise ValueError("--log-file kan bare brukes sammen med --dry-run.")
            source = existing_path_arg(args.path).resolve()
            if not source.is_dir():
                raise ValueError(f"Kilden finnes ikke som mappe: {source}")
            validate_source_target(source, target)
            with TargetLock(target, command="import"):
                source_id = db.add_named_source(conn, source, args.name)
                conn.commit()
                print(f"Registrert kilde #{source_id}: {args.name} ({source})")
                source_row = db.get_source(conn, source_id)
                if source_row.imported_at is not None:
                    print(f"Kilden er allerede importert: {args.name}")
                    return 0
                stats = import_source(conn, target, source_row, verbose=not args.quiet)
            print_summary(stats)
            return 0 if stats.errors == 0 else 2

        if args.command == "list-sources":
            for source in db.get_sources(conn):
                imported = source.imported_at or "-"
                superseded_by = source.superseded_by_source_id or "-"
                print(
                    f"{source.id}\t{source.status}\t"
                    f"{imported}\t{superseded_by}\t{source.name}\t{source.path}"
                )
            return 0

        if args.command == "status":
            print_status(conn)
            return 0

        if args.command == "conflicts":
            for row in db.name_conflicts(conn):
                target_path = db.absolute_target_path(target, Path(str(row["target_path"])))
                print(f"{row['source_path']} -> {target_path}")
            return 0

        if args.command == "show-conflict":
            path = resolve_collection_file_arg(target, args.path)
            row = db.file_by_target_path(conn, target, path)
            if row is None:
                raise ValueError(f"Filen finnes ikke i importdatabasen: {path}")
            rows = name_conflict_group(conn, row)
            if len(rows) < 2 or not any(item["name_conflict"] for item in rows):
                print(f"Filen er ikke del av en navnekollisjon: {path}")
                return 0
            print(f"Navnekollisjon: {row['original_filename']}")
            print(f"Mappe i bildesamlingen: {db.absolute_target_path(target, Path(str(row['target_path']))).parent}")
            with MediaMetadataCache(target, conn) as media_cache:
                for item in rows:
                    print_name_conflict_item(target, item, media_cache=media_cache)
            return 0

        if args.command == "show-source":
            path = resolve_collection_file_arg(target, args.path)
            rows = db.file_sources_by_target_path(conn, target, path)
            if not rows:
                raise ValueError(f"Filen finnes ikke i importdatabasen: {path}")
            print_source_items(target, rows)
            return 0

        if args.command == "unimport":
            source = resolve_source_by_name(conn, args.name)
            if source.status == "superseded":
                raise ValueError(
                    "Kan ikke unimportere en superseded kilde. "
                    f"Kilden er dekket av en annen import: {source.path}"
                )
            with TargetLock(target, command="unimport"):
                plan = db.build_unimport_plan(conn, target, source)
                validate_unimport_source_files(conn, source)
                validate_unimport_target_files(plan)
                print_unimport_plan(plan)
                if args.dry_run:
                    print_unimport_dry_run_note(source)
                    print("Dry-run: ingen endringer er gjort.")
                    return 0
                answer = input('Skriv "ja, det vil jeg" for å gjennomføre unimport: ')
                if answer != "ja, det vil jeg":
                    conn.rollback()
                    print("Avbrutt. Ingen endringer er gjort.")
                    return 0
                db.apply_unimport(conn, plan)
                for target_path in plan.target_paths_to_delete:
                    target_path.unlink()
                conn.commit()
            print("Unimport gjennomført.")
            print("Kilden er fjernet fra kildelisten.")
            return 0

        if args.command == "remove":
            original_path = resolve_target_file_arg(target, args.path)
            row = db.file_by_target_path(conn, target, original_path)
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
                target_root=target,
                deleted_path=deleted_path,
                original_target_path=original_path,
            )
            conn.commit()
            print(f"Flyttet til slettet mappe: {deleted_path}")
            return 0

        if args.command == "list-removed":
            for row in db.deleted_files(conn):
                print_deleted_item(target, row)
            return 0

        if args.command == "undelete":
            deleted_path = resolve_deleted_file_arg(target, args.path)
            row = db.file_by_target_path(conn, target, deleted_path)
            if row is None:
                raise ValueError(f"Filen finnes ikke i importdatabasen: {deleted_path}")
            if row["deleted_at"] is None:
                raise ValueError(f"Filen er ikke markert som slettet: {deleted_path}")
            if row["deleted_original_target_path"] is None:
                raise ValueError(f"Filen mangler opprinnelig målsti i databasen: {deleted_path}")
            if not deleted_path.exists():
                raise ValueError(f"Slettet fil finnes ikke på disk: {deleted_path}")

            restored_path = target / Path(str(row["deleted_original_target_path"]))
            if restored_path.exists():
                raise ValueError(f"Målfilen finnes allerede: {restored_path}")

            restored_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(deleted_path), str(restored_path))
            db.mark_file_undeleted(
                conn,
                file_id=int(row["id"]),
                target_root=target,
                restored_path=restored_path,
            )
            conn.commit()
            print(f"Flyttet tilbake til bildesamlingen: {restored_path}")
            return 0

        if args.command == "non-metadata":
            for row in db.non_metadata_files(conn):
                taken_date = row["taken_date"] or "-"
                if args.with_source:
                    print(
                        f"{row['date_source']}\t{taken_date}\t"
                        f"{db.absolute_target_path(target, Path(str(row['target_path'])))}\t{row['source_path']}"
                    )
                else:
                    print(f"{row['date_source']}\t{taken_date}\t{db.absolute_target_path(target, Path(str(row['target_path'])))}")
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
                month_preview_limit=args.month_preview_limit,
                debug_timing=args.debug,
            )
            print(f"Skrev HTML-browser: {output_path}")
            return 0

        if args.command == "make-thumbnails":
            conn.commit()
            conn.close()
            stats = run_make_thumbnails(
                target,
                limit=args.limit,
                verbose=args.verbose,
                progress=print_thumbnail_progress,
            )
            print_thumbnail_summary(stats)
            return 0 if stats.errors == 0 else 2

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

        raise ValueError(f"Ukjent kommando: {args.command}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def print_thumbnail_summary(stats: ThumbnailStats) -> None:
    print(
        "Thumbnails: "
        f"sjekket={stats.checked}, "
        f"laget={stats.created}, "
        f"ferske={stats.skipped_current}, "
        f"hoppet_over_ikke_bilde={stats.skipped_non_image}, "
        f"feil={stats.errors}"
    )
    if stats.last_error_path is not None and stats.last_error_message:
        print(f"Siste feil: {stats.last_error_path}: {stats.last_error_message}")


def print_thumbnail_progress(
    stage: str,
    current: int,
    total: int,
    stats: ThumbnailStats,
    path: Path | None,
) -> None:
    if stage == "start":
        print(f"Thumbnails: {total} filer skal kontrolleres.")
        return
    if stage == "error":
        message = stats.last_error_message or "ukjent feil"
        print(f"Thumbnail-feil: {path}\t{message}")
        return
    if stage == "check":
        if should_print_progress(current, total):
            print(
                "Thumbnails: "
                f"kontrollert={current}/{total}, "
                f"sjekket={stats.checked}, laget={stats.created}, "
                f"ferske={stats.skipped_current}, feil={stats.errors}"
            )
        return
    if stage == "done":
        print(f"Thumbnails: ferdig kontrollert {min(current, total)}/{total} filer.")
        return


def resolve_target(target_arg: Path | None) -> Path:
    if target_arg is not None:
        target = target_arg.resolve()
        if not db.db_path_for_target(target).exists():
            raise ValueError(f"Bildesamlingen er ikke initialisert: {target}")
        return target
    target = db.find_target()
    if target is None:
        raise ValueError("Fant ingen bildesamling. Kjør kommandoen fra bildesamlingsmappen.")
    return target


def resolve_source_by_name(conn, name: str) -> db.Source:
    source = db.find_source_by_name(conn, name)
    if source is None:
        raise ValueError(f"Fant ikke kilde med navn: {name}")
    return source


def validate_unimport_source_files(conn, source: db.Source) -> None:
    for row in db.source_file_sources(conn, source.id):
        source_path = Path(str(row["source_path"]))
        if not source_path.exists():
            raise ValueError(
                f"Kildefil mangler: {source_path}\n"
                "Sjekk at riktig mappe, USB-disk, CD eller minnekort er tilgjengelig, "
                "og at det har samme stasjon/path som da importen ble kjørt."
            )
        if not source_path.is_file():
            raise ValueError(f"Kildefil er ikke en fil: {source_path}")
        size_bytes = source_path.stat().st_size
        if size_bytes != int(row["size_bytes"]):
            raise ValueError(
                f"Kildefil har endret størrelse: {source_path} "
                f"(nå {size_bytes}, forventet {row['size_bytes']})"
            )
        file_hash = sha256_file(source_path)
        if file_hash != row["sha256"]:
            raise ValueError(f"Kildefil har endret innhold: {source_path}")


def validate_unimport_target_files(plan: db.UnimportPlan) -> None:
    for target_path in plan.target_paths_to_delete:
        if not target_path.exists():
            raise ValueError(f"Målfilen som skulle fjernes finnes ikke: {target_path}")
        if not target_path.is_file():
            raise ValueError(f"Målfilen som skulle fjernes er ikke en fil: {target_path}")


def print_unimport_plan(plan: db.UnimportPlan) -> None:
    print(f"Kilde: {plan.source.name or plan.source.path}")
    print(f"Registrerte kildefiler kontrollert: {plan.source_file_count}")
    print(f"Filer som fjernes fra aktiv samling: {plan.active_remove_count}")
    print(
        "Filer som blir liggende fordi de også finnes i andre kilder: "
        f"{plan.active_keep_count}"
    )


def print_unimport_dry_run_note(source: db.Source) -> None:
    print("Kilden ville blitt fjernet fra kildelisten.")


def validate_target_not_in_program_repo(target: Path) -> None:
    repo_root = program_repo_root()
    try:
        target.resolve().relative_to(repo_root)
    except ValueError:
        return
    raise ValueError(
        "Bildesamlingen kan ikke ligge inni programmappen. "
        "Velg en egen bildesamlingsmappe utenfor repoet."
    )


def program_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def run_update() -> int:
    if sys.platform == "win32":
        return run_update_windows()
    return run_update_linux()


def run_where_is() -> int:
    repo_root = program_repo_root()
    print("Bildebank-program:")
    print(f"  {repo_root}")
    print()
    print("Programdata:")
    print(f"  {program_db_path(repo_root)}")
    print()
    print("Gjeldende mappe:")
    print(f"  {Path.cwd().resolve()}")
    print()
    print("Kjente bildesamlingsmapper:")
    targets = known_targets(repo_root)
    if not targets:
        print("  Ingen registrert ennå.")
        print()
        print("Når du oppretter eller bruker en bildesamling, blir den lagt til her.")
        return 0
    for target in targets:
        exists = "finnes" if target.exists else "finnes ikke"
        last_seen = target.last_seen_at or "-"
        print(f"  {target.path}")
        print(f"    status: {exists}")
        print(f"    sist brukt: {last_seen}")
    first_existing = next((target for target in targets if target.exists), None)
    if first_existing is not None:
        print()
        print("For å jobbe med en bildesamling kan du skrive:")
        print(f'  cd "{first_existing.path}"')
    return 0


def run_backup_command(target: Path, destination: Path, *, dry_run: bool = False) -> int:
    stats = run_backup(target, destination, dry_run=dry_run)
    plan = stats.plan
    print("Source:")
    print(f"  {plan.source_dir}")
    print()
    print("Backup parent:")
    print(f"  {plan.backup_parent}")
    print()
    print("Backup directory:")
    print(f"  {plan.backup_dir}")
    print()
    print("Mode:")
    print("  Dry run" if dry_run else "  Backup")
    print()
    print("Result:")
    if dry_run:
        action = "Would update backup." if plan.existing_backup else "Would create new backup."
        print(f"  {action}")
        print(f"  motor={stats.engine}")
        if stats.warning:
            print(f"  ADVARSEL: {stats.warning}")
        return 0
    action = "Updated backup." if plan.existing_backup else "Created new backup."
    print(f"  {action}")
    print(f"  motor={stats.engine}")
    if stats.warning:
        print(f"  ADVARSEL: {stats.warning}")
    if stats.stats_available:
        print(
            "  "
            f"files_copied={stats.files_copied}, files_deleted={stats.files_deleted}, "
            f"dirs_created={stats.dirs_created}, dirs_deleted={stats.dirs_deleted}"
        )
    return 0


def run_geo_scan(
    target: Path,
    *,
    force: bool,
    only_missing: bool,
    limit: int | None,
    verbose: bool,
    exiftool_path: Path | None,
    batch_size: int,
) -> int:
    if force and only_missing:
        raise ValueError("--force og --only-missing kan ikke brukes samtidig.")
    stats = scan_geo(
        target,
        force=force,
        only_missing=only_missing,
        limit=limit,
        verbose=verbose,
        exiftool_path=exiftool_path.resolve() if exiftool_path else None,
        batch_size=batch_size,
    )
    print("Scanning GPS metadata...")
    print(f"Images checked: {stats.checked}")
    print(f"With GPS:        {stats.with_gps}")
    print(f"Without GPS:     {stats.without_gps}")
    print(f"Errors:          {stats.errors}")
    print(f"Updated:         {stats.updated}")
    return 0 if stats.errors == 0 else 2


def run_geo_stats(target: Path) -> int:
    conn = db.connect(target)
    try:
        stats = db.geo_stats(conn)
    finally:
        conn.close()
    print(f"Images total:             {stats['total']}")
    print(f"Images scanned for GPS:   {stats['scanned']}")
    print(f"Images with GPS:          {stats['with_gps']}")
    print(f"Images without GPS:       {stats['without_gps']}")
    print(f"Images with GPS errors:   {stats['errors']}")
    return 0


def run_geo_areas(target: Path, *, resolution: int, min_count: int, limit: int) -> int:
    column = h3_column_for_resolution(resolution)
    conn = db.connect(target)
    try:
        rows = db.geo_areas(conn, column=column, min_count=min_count, limit=limit)
    finally:
        conn.close()
    print(f"Resolution: {resolution}")
    print()
    print("Count  H3 cell")
    print("-----  ---------------")
    for row in rows:
        print(f"{int(row['count']):5d}  {row['h3_cell']}")
    return 0


def run_geo_area(
    target: Path,
    *,
    h3_cell: str,
    limit: int | None,
    with_date: bool,
    with_coordinates: bool,
) -> int:
    resolution = h3_resolution(h3_cell)
    column = h3_column_for_resolution(resolution)
    conn = db.connect(target)
    try:
        rows = db.geo_area_files(conn, column=column, h3_cell=h3_cell, limit=limit)
    finally:
        conn.close()

    print(f"H3 cell: {h3_cell}")
    print(f"Images: {len(rows)}")
    print()
    for row in rows:
        parts = [str(row["target_path"])]
        if with_date:
            parts.append(row["taken_date"] or "-")
        if with_coordinates:
            lat = row["gps_lat"]
            lon = row["gps_lon"]
            parts.append("-" if lat is None or lon is None else f"{float(lat):.6f}, {float(lon):.6f}")
        print("\t".join(parts))
    return 0


def run_face_status(target_arg: Path | None = None) -> int:
    repo_root = program_repo_root()
    config = load_config(repo_root)
    face = config.face_recognition
    print("Ansiktsgjenkjenning:")
    print(f"  konfigurert: {'på' if face.enabled else 'av'}")
    print(f"  config-fil: {repo_root / CONFIG_FILENAME}")
    print(f"  modellmappe: {face.model_root}")
    print(f"  modellnavn: {face.model_name}")
    print(f"  provider: {face.provider}")
    print(f"  insightface installert: {module_available('insightface')}")
    print(f"  onnxruntime installert: {module_available('onnxruntime')}")
    print()
    print("Tekstbasert bildesøk:")
    print(f"  konfigurert: {'på' if config.openclip.enabled else 'av'}")
    print(f"  modellmappe: {config.openclip.model_root}")
    print(f"  modellnavn: {config.openclip.model_name}")
    print(f"  pretrained: {config.openclip.pretrained}")
    print(f"  device-valg: {config.openclip.device}")
    print(f"  open_clip installert: {module_available('open_clip')}")
    gpu_status = torch_gpu_status()
    print(f"  torch installert: {gpu_status['torch']}")
    print(f"  cuda/gpu funnet: {gpu_status['cuda']}")
    print(f"  gpu-navn: {gpu_status['device']}")
    target = db.find_target(target_arg)
    if target is not None:
        exists, scanned, faces = face_db_summary(target)
        openclip_summary = openclip_db_summary(target)
        print()
        print("Aktiv bildesamling:")
        print(f"  {target}")
        print(f"  face-database: {face_db_path(target)}")
        print(f"  face-database finnes: {'ja' if exists else 'nei'}")
        print(f"  scannede filer: {scanned}")
        print(f"  ansikter funnet: {faces}")
        print(f"  openclip-database: {openclip_db_path(target)}")
        print(f"  openclip-database finnes: {'ja' if openclip_summary.exists else 'nei'}")
        print(f"  bilde-embeddings: {openclip_summary.embeddings}")
        print(f"  bildesøk: {openclip_summary.search_runs}")
    return 0


def module_available(module_name: str) -> str:
    return "ja" if importlib.util.find_spec(module_name) is not None else "nei"


def run_image_scan(target: Path, *, limit: int | None) -> int:
    config = load_config(program_repo_root()).openclip
    stats = scan_images(target, config, limit=limit, progress=print_image_scan_progress)
    print(
        "Bildesøk-scan: "
        f"bilder={stats.total}, hoppet_over={stats.skipped}, "
        f"scannet={stats.scanned}, feil={stats.errors}"
    )
    print(f"OpenCLIP-database: {openclip_db_path(target)}")
    return 0 if stats.errors == 0 else 2


def print_image_scan_progress(
    stage: str,
    current: int,
    total: int,
    stats,
    path: Path | None,
) -> None:
    global IMAGE_SCAN_PROGRESS_STARTED_AT
    if stage == "start":
        IMAGE_SCAN_PROGRESS_STARTED_AT = None
        print(f"Image-scan: {total} bildefiler skal kontrolleres.")
        return
    if stage == "load_model":
        IMAGE_SCAN_PROGRESS_STARTED_AT = None
        print(f"Image-scan: {stats.to_scan} nye eller endrede bilder skal scannes.")
        print("Image-scan: laster OpenCLIP-modell. Det kan ta litt tid.")
        return
    if stage == "error":
        message = getattr(stats, "last_error_message", None) or "ukjent feil"
        print(f"Image-scan-feil: {path}\t{message}")
        return
    if stage == "check":
        if should_print_progress(current, total):
            print(
                "Image-scan: "
                f"kontrollert={current}/{total}, "
                f"hoppet_over={stats.skipped}, skal_scannes={stats.to_scan}"
            )
        return
    if stage == "scan":
        if IMAGE_SCAN_PROGRESS_STARTED_AT is None and current > 0:
            IMAGE_SCAN_PROGRESS_STARTED_AT = time.monotonic()
        if should_print_progress(current, total):
            eta = face_scan_eta_text(current, total, IMAGE_SCAN_PROGRESS_STARTED_AT)
            print(
                "Image-scan: "
                f"behandlet={stats.skipped + current}/{stats.total}, "
                f"scannet={current}/{total}, hoppet_over={stats.skipped}, "
                f"feil={stats.errors}, gjenstår={eta}"
            )
        return


def run_image_search(target: Path, *, query: str, limit: int, browser: bool = True) -> int:
    config = load_config(program_repo_root()).openclip
    stats = search_images(target, config, query=query, limit=limit, progress=print_image_search_progress)
    print(f"Søk: {stats.query}")
    print(f"Treff: {len(stats.results)}")
    for result in stats.results[:20]:
        print(f"{result.rank}\tscore={result.similarity:.3f}\t{result.target_path}")
    if len(stats.results) > 20:
        print(f"... {len(stats.results) - 20} flere treff i HTML-filen")
    print(f"Skrev bildesøk: {stats.output_path}")
    if browser:
        open_file_in_browser(stats.output_path)
        print(f"Åpnet bildesøk: {stats.output_path}")
    return 0


def run_server_command(target: Path, *, host: str, port: int, browser: bool = True) -> int:
    config = load_config(program_repo_root())
    print("Starter Bildebank-server. Dette kan ta noen sekunder.")
    print(f"Bildesamling: {target}")

    def on_ready(url: str) -> None:
        print(f"Bildebank-serveren er klar: {url}")
        print("Trykk Ctrl-C for å stoppe serveren.")
        if browser:
            print("Åpner nettleser.")
            webbrowser.open(url)

    run_local_server(target, config, host=host, port=port, ready=on_ready)
    return 0


def print_image_search_progress(
    stage: str,
    current: int,
    total: int,
    stats,
) -> None:
    if stage == "load_model":
        print(f"Image-search: fant {total} bilde-embeddings. Laster OpenCLIP-modell.")
        return
    if stage == "compare_start":
        print(f'Image-search: søker etter "{stats.query}" i {total} bilder.')
        return
    if stage == "compare":
        if should_print_progress(current, total):
            print(f"Image-search: søkt={current}/{total}")
        return
    if stage == "write":
        print(f"Image-search: skriver {current} treff til image-search.html.")
        return


def run_face_scan(target: Path, *, limit: int | None, show_model_output: bool = False) -> int:
    config = load_config(program_repo_root()).face_recognition
    require_face_enabled(config.enabled)
    stats = scan_faces(
        target,
        config,
        limit=limit,
        progress=print_face_scan_progress,
        show_model_output=show_model_output,
    )
    print(
        "Oppsummering: "
        f"sjekket={stats.checked}, hoppet_over={stats.skipped}, "
        f"scannet={stats.scanned}, ansikter={stats.faces}, feil={stats.errors}"
    )
    print(f"Face-database: {face_db_path(target)}")
    return 0 if stats.errors == 0 else 2


def print_face_scan_progress(
    stage: str,
    current: int,
    total: int,
    stats,
    path: Path | None,
) -> None:
    global FACE_SCAN_PROGRESS_STARTED_AT
    if stage == "start":
        FACE_SCAN_PROGRESS_STARTED_AT = None
        print(f"Face-scan: {total} bildefiler skal kontrolleres.")
        return
    if stage == "check":
        if should_print_progress(current, total):
            print(
                "Face-scan: "
                f"kontrollert={current}/{total}, "
                f"hoppet_over={stats.skipped}, skal_scannes={stats.checked - stats.skipped}"
            )
        return
    if stage == "load_model":
        FACE_SCAN_PROGRESS_STARTED_AT = None
        print(f"Face-scan: {total} nye eller endrede bilder skal scannes.")
        print("           Laster ansiktsmodell. Det kan ta 20 sekunder eller mer.")
        return
    if stage == "error":
        message = getattr(stats, "last_error_message", None) or "ukjent feil"
        print(f"Face-scan-feil: {path}\t{message}")
        return
    if stage == "scan":
        if FACE_SCAN_PROGRESS_STARTED_AT is None:
            FACE_SCAN_PROGRESS_STARTED_AT = time.monotonic()
        if should_print_progress(current, total):
            eta = face_scan_eta_text(current, total, FACE_SCAN_PROGRESS_STARTED_AT)
            print(
                "Face-scan: "
                f"scannet={current}/{total}, "
                f"ansikter={stats.faces}, feil={stats.errors}, "
                f"gjenstår={eta}"
            )
        return


def should_print_progress(current: int, total: int) -> bool:
    return total <= 20 or current == total or current % 25 == 0


def face_scan_eta_text(current: int, total: int, started_at: float | None) -> str:
    if started_at is None or current <= 0:
        return "ukjent"
    remaining = total - current
    if remaining <= 0:
        return "0s"
    elapsed = max(time.monotonic() - started_at, 0.0)
    if current < 3 and elapsed < 5.0:
        return "beregner"
    seconds = elapsed * remaining / current
    return format_duration(seconds)


def format_duration(seconds: float) -> str:
    seconds = max(int(round(seconds)), 0)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}t {minutes:02d}m"


def run_face_report(target: Path, *, limit: int) -> int:
    report = face_report(target, limit=limit)
    print_face_report(target, report)
    return 0


def run_face_suggest(target: Path, *, threshold: float) -> int:
    stats = suggest_faces(target, threshold=threshold)
    print_face_suggest_stats(stats)
    print("Dette er forslag basert på personer du allerede har bekreftet.")
    return 0


def print_add_face_to_person_result(result: AddFaceToPersonResult) -> None:
    print(f"Person: {result.person_name}")
    print(f"Ansikt-id: {result.face_id}")
    if result.added:
        print("Ansiktet er koblet til personen.")
    else:
        print("Ansiktet var allerede koblet til personen.")


def print_remove_face_from_person_result(result: RemoveFaceFromPersonResult) -> None:
    print(f"Person: {result.person_name}")
    print(f"Ansikt-id: {result.face_id}")
    if result.removed:
        print("Ansiktet er fjernet fra personen.")
    else:
        print("Ansiktet var ikke koblet til personen.")


def run_face_person_delete(target: Path, name: str) -> int:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Personnavn kan ikke være tomt.")
    answer = input(f'Skriv "slett {clean_name}" for å slette personen fra ansiktsdatabasen: ')
    if answer != f"slett {clean_name}":
        print("Avbrutt. Ingen endringer er gjort.")
        return 0
    result = delete_person(target, clean_name)
    print_delete_person_result(result)
    return 0


def print_delete_person_result(result: DeletePersonResult) -> None:
    print(f"Slettet person: {result.person_name}")
    print(f"Fjernet bekreftede ansiktskoblinger: {result.removed_faces}")
    print(f"Fjernet ansiktsforslag: {result.removed_suggestions}")
    print("Ingen bilder eller scannede ansikter er slettet.")


def print_face_suggest_stats(stats: FaceSuggestStats) -> None:
    print(
        "Ansiktsforslag: "
        f"personer={stats.persons}, ukjente_ansikter={stats.unknown_faces}, "
        f"forslag={stats.suggestions}, threshold={stats.threshold:.3f}"
    )


def print_face_suggestions(target: Path) -> None:
    rows = list_face_suggestions(target)
    if not rows:
        print("Ingen forslag.")
        return
    print("Forslag:")
    for row in rows:
        target_path = db.absolute_target_path(target, Path(str(row["target_path"])))
        relative_path = db.target_relative_path(target, target_path).as_posix()
        print(
            f"  {row['name']}\tface-id={row['face_id']}\t"
            f"score={float(row['similarity']):.3f}\t{relative_path}"
        )


def print_persons(target: Path) -> None:
    rows = list_persons(target)
    if not rows:
        print("Ingen personer registrert.")
        return
    for row in rows:
        print(
            f"{row['name']}"
            f"\tbekreftede_bilder={row['confirmed_file_count']}"
            f"\tbekreftede_ansikter={row['face_count']}"
            f"\tforslag={row['suggestion_count']}"
            f"\toppdatert={row['updated_at']}"
        )


def print_face_report(target: Path, report: FaceReport) -> None:
    print("Ansiktsrapport")
    print(f"Bildesamling: {target}")
    print(f"Face-database: {face_db_path(target)}")
    if not report.database_exists:
        print("Face-database finnes ikke.")
        print("Kjør bildebank face-scan først.")
        return
    print(f"Scannede filer: {report.scanned_files}")
    print(f"Ansikter funnet: {report.total_faces}")
    print(f"Filer uten ansikter: {report.files_with_zero_faces}")
    print(f"Filer med ett ansikt: {report.files_with_one_face}")
    print(f"Filer med flere ansikter: {report.files_with_multiple_faces}")
    print(f"Scan-feil: {report.scan_errors}")
    print()
    print("Personstatus:")
    print(f"  Personer registrert: {report.persons}")
    print(f"  Bekreftede ansiktskoblinger: {report.confirmed_face_links}")
    print(f"  Forslag: {report.suggestions}")
    print(f"  Bilder med minst én bekreftet person: {report.files_with_confirmed_person}")
    print(f"  Bilder med ansikter, men ingen bekreftet person: {report.files_with_faces_no_confirmed_person}")
    print(f"  Bilder med både bekreftede og ukjente ansikter: {report.files_with_confirmed_and_unknown_faces}")
    if report.top_files:
        print()
        print("Flest ansikter:")
        for row in report.top_files:
            target_path = db.absolute_target_path(target, Path(str(row["target_path"])))
            print(f"  {row['face_count']}\t{db.target_relative_path(target, target_path).as_posix()}")
    if report.errors:
        print()
        print("Siste scan-feil:")
        for row in report.errors:
            target_path = db.absolute_target_path(target, Path(str(row["target_path"])))
            print(f"  {db.target_relative_path(target, target_path).as_posix()}\t{row['error_message']}")


def run_face_reset(
    target: Path,
    *,
    all_data: bool = False,
) -> int:
    path = face_db_path(target)
    if not path.exists():
        print(f"Fant ingen face-database: {path}")
        return 0
    mode = face_reset_mode(
        all_data=all_data,
    )
    phrase = face_reset_confirmation_phrase(mode)
    print(face_reset_description(mode))
    answer = input(f'Skriv "{phrase}" for å gjennomføre face-reset: ')
    if answer != phrase:
        print("Avbrutt. Ingen endringer er gjort.")
        return 0
    if mode == "all":
        path.unlink()
        print(f"Slettet face-database: {path}")
        print("Alle ansiktsdata er slettet.")
        return 0
    result = reset_face_database(target, mode=mode)
    print_face_reset_result(result)
    return 0


def face_reset_mode(*, all_data: bool) -> str:
    if all_data:
        return "all"
    return "keep-scan"


def face_reset_confirmation_phrase(mode: str) -> str:
    if mode == "all":
        return "ja, slett ansiktsdata"
    if mode == "keep-scan":
        return "ja, slett personer"
    raise ValueError(f"Ukjent face-reset-nivå: {mode}")


def face_reset_description(mode: str) -> str:
    if mode == "all":
        return (
            "Dette sletter hele face-databasen: face-scan-resultater, "
            "personer, bekreftelser og forslag."
        )
    if mode == "keep-scan":
        return (
            "Dette beholder face-scan-resultater, men sletter personer, "
            "bekreftelser og forslag."
        )
    raise ValueError(f"Ukjent face-reset-nivå: {mode}")


def print_face_reset_result(result: FaceResetResult) -> None:
    if result.mode == "keep-scan":
        print("Face-reset gjennomført. Face-scan-resultater er beholdt.")
    else:
        print("Face-reset gjennomført.")
    print(f"Slettet personer: {result.removed_persons}")
    print(f"Slettet bekreftede ansiktskoblinger: {result.removed_person_faces}")
    print(f"Slettet ansiktsforslag: {result.removed_suggestions}")


def require_face_enabled(enabled: bool) -> None:
    if not enabled:
        raise ValueError(
            f"Ansiktsgjenkjenning er av. Sett enabled = true i {CONFIG_FILENAME} "
            "hvis du vil teste."
        )


def require_openclip_enabled(enabled: bool) -> None:
    if not enabled:
        raise ValueError(
            f"Tekstbasert bildesøk er av. Sett enabled = true under [openclip] i {CONFIG_FILENAME} "
            "hvis du vil teste."
        )


def run_named_import_dry_run(target: Path, args: argparse.Namespace) -> int:
    if not args.name or args.path is None:
        raise ValueError('Bruk både --name og mappe: bildebank import --name "Navn" "path\\til\\kilde"')
    source = existing_path_arg(args.path).resolve()
    if not source.is_dir():
        raise ValueError(f"Kilden finnes ikke som mappe: {source}")
    validate_source_target(source, target)
    output_path = args.log_file.resolve() if args.log_file else None
    conn = db.connect(target)
    try:
        existing = db.find_source_by_name(conn, args.name)
        if existing is not None and existing.imported_at is not None:
            raise ValueError(
                f"Kilde med navn {args.name!r} er allerede importert som "
                f"{existing.path}. Bruk et nytt --name hvis dette er en annen mappe/import."
            )
        source_row = db.Source(
            id=0,
            path=source,
            path_key=None,
            name=args.name,
            imported_at=None,
            status="dry-run",
            superseded_by_source_id=None,
        )
        if output_path is None:
            stats = import_source_dry_run(
                conn, target, source_row, output=sys.stdout, verbose=not args.quiet
            )
        else:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with output_path.open("w", encoding="utf-8", newline="\n") as output:
                stats = import_source_dry_run(
                    conn, target, source_row, output=output, verbose=not args.quiet
                )
            print(f"Skrev dry-run importliste: {output_path}")
        print_summary(stats)
        return 0 if stats.errors == 0 else 2
    finally:
        conn.close()


def run_migrate(target: Path, *, check: bool) -> int:
    plan = db.migration_plan(target, validate=check)
    print(f"Bildesamling: {target}")
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
    if plan.rebuilds_files_without_legacy_source_columns:
        print("  bygge om files uten gamle v1-kildekolonner")
    if plan.drops_duplicate_findings:
        print("  fjerne legacy-tabellen duplicate_findings")
    if plan.rebuilds_errors_without_source_fk:
        print("  bygge om errors slik at source-historikk ikke blokkerer sletting")
    if plan.rebuilds_sources_without_kind:
        print("  bygge om sources uten kind og gi alle kilder navn")
    if plan.rebuilds_file_sources_without_kind:
        print("  bygge om file_sources uten kind")
    print("Vil lage backup før endring.")
    if check:
        print("Ingen endringer er gjort (--check).")
        return 0

    with TargetLock(target, command="migrate"):
        print("Låser bildesamling.")
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
    if result.rebuilds_files_without_legacy_source_columns:
        print("Bygger om files uten gamle v1-kildekolonner.")
    if result.drops_duplicate_findings:
        print("Fjerner legacy-tabellen duplicate_findings.")
    if result.rebuilds_errors_without_source_fk:
        print("Bygger om errors uten foreign key til sources.")
    if result.rebuilds_sources_without_kind:
        print("Bygger om sources uten kind og med name NOT NULL.")
    if result.rebuilds_file_sources_without_kind:
        print("Bygger om file_sources uten kind.")
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


def resolve_collection_file_arg(target: Path, path: Path) -> Path:
    candidate = existing_path_arg(path)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (target / candidate).resolve()
    relative_collection_path(target, resolved)
    return resolved


def resolve_target_file_arg(target: Path, path: Path) -> Path:
    resolved = resolve_collection_file_arg(target, path)
    relative_path_under_target(target, resolved)
    return resolved


def resolve_deleted_file_arg(target: Path, path: Path) -> Path:
    resolved = resolve_collection_file_arg(target, path)
    relative_path = relative_collection_path(target, resolved)
    if len(relative_path.parts) < 2 or relative_path.parts[0] != "deleted":
        raise ValueError(f"Undelete krever sti under deleted/: {path}")
    return resolved


def relative_collection_path(target: Path, path: Path) -> Path:
    try:
        relative_path = path.resolve().relative_to(target.resolve())
    except ValueError as exc:
        raise ValueError(f"Filen ligger ikke i bildesamlingen: {path}") from exc
    return relative_path


def relative_path_under_target(target: Path, path: Path) -> Path:
    relative_path = relative_collection_path(target, path)
    if not relative_path.parts or relative_path.parts[0] == "deleted":
        raise ValueError(f"Kan ikke slette filer fra deleted/: {path}")
    return relative_path


def name_conflict_group(conn, row) -> list:
    parent_key = db.relative_path_key(Path(str(row["target_path"])).parent)
    rows = []
    for candidate in db.files_by_original_filename(conn, str(row["original_filename"])):
        if db.relative_path_key(Path(str(candidate["target_path"])).parent) == parent_key:
            rows.append(candidate)
    return rows


def print_name_conflict_item(target: Path, row, *, media_cache: MediaMetadataCache | None = None) -> None:
    target_path = db.absolute_target_path(target, Path(str(row["target_path"])))
    source_path = Path(str(row["source_path"]))
    dimensions = media_cache.image_dimensions(target_path) if media_cache is not None else cached_image_dimensions(target, target_path)
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


def print_source_item(target: Path, row) -> None:
    source_path = Path(str(row["source_path"]))
    source_label = row["source_name"] or row["source_root"]
    print(f"Målfil: {db.absolute_target_path(target, Path(str(row['target_path'])))}")
    print(f"Kildefil: {source_path}")
    print(f"Kildefil finnes: {'ja' if source_path.exists() else 'nei'}")
    print(f"Kilde-id: {row['source_id']}")
    print(f"Kilde: {source_label}")
    print(f"Kildestatus: {row['source_status']}")
    print(f"Originalt filnavn: {row['original_filename']}")
    print(f"Lagret filnavn: {row['stored_filename']}")
    print(f"Importert: {row['file_imported_at']}")
    print(f"Dato: {row['taken_date'] or '-'} ({row['date_source']})")
    print(f"Filstørrelse: {format_bytes(int(row['size_bytes']))} ({row['size_bytes']} bytes)")
    print(f"SHA-256: {row['sha256']}")


def print_source_items(target: Path, rows: list) -> None:
    if len(rows) == 1:
        print_source_item(target, rows[0])
        return

    first = rows[0]
    print(f"Målfil: {db.absolute_target_path(target, Path(str(first['target_path'])))}")
    print(f"Originalt filnavn: {first['original_filename']}")
    print(f"Lagret filnavn: {first['stored_filename']}")
    print(f"Importert: {first['file_imported_at']}")
    print(f"Dato: {first['taken_date'] or '-'} ({first['date_source']})")
    print(f"Filstørrelse: {format_bytes(int(first['size_bytes']))} ({first['size_bytes']} bytes)")
    print(f"SHA-256: {first['sha256']}")
    print("Kildefiler:")
    for row in rows:
        source_path = Path(str(row["source_path"]))
        source_label = row["source_name"]
        print(f"- {source_path}")
        print(f"  finnes: {'ja' if source_path.exists() else 'nei'}")
        print(f"  kilde-id: {row['source_id']}")
        print(f"  kilde: {source_label}")
        print(f"  kildestatus: {row['source_status']}")


def print_deleted_item(target: Path, row) -> None:
    deleted_path = db.absolute_target_path(target, Path(str(row["target_path"])))
    original_path = (
        db.absolute_target_path(target, Path(str(row["deleted_original_target_path"])))
        if row["deleted_original_target_path"] is not None
        else "-"
    )
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


def non_negative_int_arg(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("må være et heltall") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("må være minst 0")
    return number


def h3_resolution_arg(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("må være et heltall") from exc
    if number not in {5, 6, 7, 8, 9}:
        raise argparse.ArgumentTypeError("må være 5, 6, 7, 8 eller 9")
    return number


def similarity_threshold_arg(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("må være et tall mellom 0.0 og 1.0") from exc
    if not 0.0 <= number <= 1.0:
        raise argparse.ArgumentTypeError("må være mellom 0.0 og 1.0")
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
