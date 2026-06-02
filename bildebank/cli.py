from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import traceback
import webbrowser
from dataclasses import dataclass, replace
from pathlib import Path

from . import __version__, db
from .backup import run_backup
from .config import CONFIG_FILENAME, load_config, set_config_enabled
from .exiftool import install_managed_exiftool, resolve_exiftool_path, validate_exiftool_install
from .exiftool_probe import exiftool_metadata_gaps
from .face import (
    AddFaceToPersonResult,
    DeletePersonResult,
    FaceResetResult,
    FaceReport,
    FaceSuggestStats,
    RemoveFaceFromPersonResult,
    RenamePersonResult,
    add_face_to_person,
    create_person,
    delete_person,
    export_face_browser,
    export_people_browser,
    export_person_browser,
    face_db_path,
    face_db_summary,
    face_report,
    list_persons,
    remove_face_from_person,
    rename_person,
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
    WalkError,
    import_source,
    import_source_dry_run,
    iter_media_files,
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
from .progress import ProgressMeter
from .program_state import known_targets, program_db_path, record_target_best_effort
from .server import DEFAULT_HOST, DEFAULT_PORT, run_server as run_local_server
from .target_lock import TargetLock
from .thumbnails import ThumbnailStats, run_make_thumbnails


THUMBNAIL_PROGRESS: ProgressMeter | None = None
IMAGE_SCAN_PROGRESS: ProgressMeter | None = None
IMAGE_SEARCH_PROGRESS: ProgressMeter | None = None
FACE_SCAN_PROGRESS: ProgressMeter | None = None
FACE_SUGGEST_PROGRESS: ProgressMeter | None = None
REFRESH_METADATA_PROGRESS: ProgressMeter | None = None
UNIMPORT_SOURCE_PROGRESS: ProgressMeter | None = None
UNIMPORT_TARGET_PROGRESS: ProgressMeter | None = None
CHECK_SOURCE_PROGRESS: ProgressMeter | None = None


HELP_COMMAND_GROUPS = (
    (
        "kom i gang",
        (
            ("create", "Opprett en ny bildesamlingsmappe"),
            ("import", "Importer en mappe, CD, USB eller annen kilde"),
            ("config", "Slå valgfrie funksjoner på eller av"),
            ("run-server", "Start lokal Bildebank-server"),
            ("status", "Vis kort status for bildesamlingen"),
            ("list-sources", "Vis registrerte kilder"),
        ),
    ),
    (
        "kontrollere importen",
        (
            ("errors", "List registrerte feil"),
            ("conflicts", "List filer med navnekollisjon"),
            ("show-conflict", "Vis detaljer om en navnekollisjon"),
            ("non-metadata", "List filer der datoen ikke kom fra metadata"),
            ("show-source", "Vis hvor en importert fil kom fra"),
            ("check-source", "Kontroller at en kildemappe er importert"),
        ),
    ),
    (
        "rydde trygt",
        (
            ("remove", "Flytt en importert fil til deleted/"),
            ("undelete", "Flytt en slettet fil tilbake fra deleted/"),
            ("unimport", "Reverser en tidligere importert kilde"),
            ("list-removed", "List filer som er slettet med `remove`"),
        ),
    ),
    (
        "metadata og steder",
        (
            ("refresh-metadata", "Sjekk filer uten metadata på nytt"),
            ("inspect-metadata", "Vis metadatafragmenter og datokandidater"),
            ("explain-date", "Forklar hvilken dato Bildebank ville brukt"),
            ("date-set", "Sett manuell dato for en importert fil"),
            ("date-clear", "Fjern manuell dato fra en importert fil"),
            ("exiftool-install", "Installer ExifTool for GPS og metadata"),
            ("exiftool-metadata-gaps", "Finn metadata Bildebank ikke leser ennå"),
            ("tag-list", "List tagger eller tagger for én fil"),
            ("tag-add", "Legg tagg på en importert fil"),
            ("tag-remove", "Fjern tagg fra en importert fil"),
            ("tag-files", "List filer med en tagg"),
            ("geo-scan", "Scan GPS-koordinater fra metadata"),
            ("geo-stats", "Vis GPS-status for bildesamlingen"),
            ("geo-areas", "List H3-områder med bilder"),
            ("geo-area", "List bilder i ett H3-område"),
        ),
    ),
    (
        "ansikter",
        (
            ("face-status", "Gammelt navn for doctor"),
            ("face-config", "Slå ansiktsgjenkjenning på eller av"),
            ("face-scan", "Scanning etter ansikter"),
            ("face-suggest", "Foreslå personer for ukjente ansikter"),
            ("face-report", "Vis rapport for scannede ansikter"),
            ("face-person-list", "List personer i ansiktsdatabasen"),
            ("face-person-create", "Opprett person i ansiktsdatabasen"),
            ("face-person-add-face", "Koble ett ansikt til person"),
            ("face-person-remove-face", "Fjern ett ansikt fra person"),
            ("face-person-delete", "Slett person fra ansiktsdatabasen"),
            ("face-person-rename", "Endre navn på person i ansiktsdatabasen"),
            ("face-reset", "Slett ansiktsdata"),
        ),
    ),
    (
        "bildesøk",
        (
            ("image-scan", "Scan bilder for tekstbasert bildesøk"),
            ("image-search", "Søk etter bilder med tekst"),
        ),
    ),
    (
        "HTML-eksport",
        (
            ("make-thumbnails", "Lag thumbnails for rask månedsvisning"),
            ("make-browser", "Lag index.html for nettleseren"),
            ("make-conflict-browser", "Lag HTML-side for navnekollisjoner"),
            ("make-face-browser", "Lag HTML-side for scannede ansikter"),
            ("make-people-browser", "Lag HTML-sider for alle personer"),
            ("make-person-browser", "Lag HTML-side for en person"),
        ),
    ),
    (
        "vedlikehold",
        (
            ("doctor", "Vis diagnose for installasjon og aktiv bildesamling"),
            ("backup", "Lag eller oppdater backup av bildesamlingen"),
            ("migrate", "Oppgrader databasen etter programoppdatering"),
            ("vacuum", "Pakk databasen så SQLite-filen krymper fysisk"),
            ("update", "Oppdater programinstallasjonen"),
            ("where-is", "Vis hvor Bildebank og kjente bildesamlinger ligger"),
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
        description="Opprette en bildesamling i mappen du velger",
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
        help="Vis importoppsummering uten å kopiere filer eller endre databasen",
    )
    imp.add_argument("path", metavar="mappe", type=Path, help="Kilden som skal importeres")

    add_command(
        subparsers,
        "list-sources",
        usage="bildebank list-sources [valg]",
        description="List registrerte kilder",
        help="List registrerte kilder",
    )
    add_command(
        subparsers,
        "status",
        usage="bildebank status [valg]",
        description="Vis kort status for bildesamlingen",
        help="Vis kort status for bildesamlingen",
    )
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
        description="Vis hvilken kilde en importert målfil kommer fra",
    )
    show_source.add_argument("path", metavar="fil", type=Path, help="Importert målfil")
    date_set = add_command(
        subparsers,
        "date-set",
        usage="bildebank date-set [valg] fil (--date DATO | --between FRA TIL)",
        help="Sett manuell dato for en importert fil",
        description="Sett manuell dato i Bildebank uten å endre originalfilen.",
    )
    date_set.add_argument("path", metavar="fil", type=Path, help="Importert målfil")
    date_choice = date_set.add_mutually_exclusive_group(required=True)
    date_choice.add_argument("--date", type=iso_date_arg, help="Eksakt eller omtrentlig midtdato, YYYY-MM-DD")
    date_choice.add_argument(
        "--between",
        nargs=2,
        metavar=("FRA", "TIL"),
        type=iso_date_arg,
        help="Usikkert datointervall, YYYY-MM-DD YYYY-MM-DD",
    )
    date_set.add_argument(
        "--uncertainty",
        help="Usikkerhet rundt --date, for eksempel 3d, 2w, 1m eller 1y",
    )
    date_set.add_argument("--note", help="Fritekstnotat om hvorfor datoen er satt")
    date_clear = add_command(
        subparsers,
        "date-clear",
        usage="bildebank date-clear [valg] fil",
        help="Fjern manuell dato fra en importert fil",
        description="Fjern manuell dato fra Bildebank uten å endre originalfilen.",
    )
    date_clear.add_argument("path", metavar="fil", type=Path, help="Importert målfil")
    check_source = add_command(
        subparsers,
        "check-source",
        usage="bildebank check-source [valg] mappe",
        help="Kontroller at en kildemappe er importert",
        description=(
            "Scanner en kildemappe og kontrollerer at alle filer i kildemappen "
            "finnes i bildesamlingen med samme SHA-256. Kommandoen sletter ingenting."
        ),
    )
    check_source.add_argument("--quiet", action="store_true", help="Ikke vis fremdrift under kontrollen")
    check_source.add_argument("path", metavar="mappe", type=Path, help="Kildemappen som skal kontrolleres")
    unimport = add_command(
        subparsers,
        "unimport",
        usage="bildebank unimport [valg] --name navn",
        help="Reverser en tidligere importert kilde",
        description=(
            "Reverser en tidligere import. "
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
        help="Flytt en importert fil til deleted/",
        description="Flytt en importert målfil til deleted/ og marker den som slettet",
    )
    remove.add_argument("path", metavar="fil", type=Path, help="Importert målfil som skal fjernes")
    undelete = add_command(
        subparsers,
        "undelete",
        usage="bildebank undelete [valg] fil",
        help="Flytt en slettet fil tilbake fra deleted/",
        description="Gjenopprett et bilde du har slettet med `remove`",
    )
    undelete.add_argument("path", metavar="fil", type=Path, help="Slettet fil under deleted/")
    add_command(
        subparsers,
        "list-removed",
        usage="bildebank list-removed [valg]",
        help="List filer som er slettet med `remove`",
        description="List filener som er slettet med `remove`",
    )
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
    tag_list = add_command(
        subparsers,
        "tag-list",
        usage="bildebank tag-list [valg] [fil]",
        help="List tagger eller tagger for én fil",
    )
    tag_list.add_argument("path", metavar="fil", type=Path, nargs="?", help="Importer målfil")
    tag_add = add_command(
        subparsers,
        "tag-add",
        usage="bildebank tag-add [valg] fil tagg",
        help="Legg tagg på en importert fil",
    )
    tag_add.add_argument("path", metavar="fil", type=Path, help="Importert målfil")
    tag_add.add_argument("tag", metavar="tagg", help="Taggnavn")
    tag_remove = add_command(
        subparsers,
        "tag-remove",
        usage="bildebank tag-remove [valg] fil tagg",
        help="Fjern tagg fra en importert fil",
    )
    tag_remove.add_argument("path", metavar="fil", type=Path, help="Importert målfil")
    tag_remove.add_argument("tag", metavar="tagg", help="Taggnavn")
    tag_files = add_command(
        subparsers,
        "tag-files",
        usage="bildebank tag-files [valg] tagg",
        help="List filer med en tagg",
    )
    tag_files.add_argument("tag", metavar="tagg", help="Taggnavn")
    exiftool_gaps = add_command(
        subparsers,
        "exiftool-metadata-gaps",
        usage="bildebank exiftool-metadata-gaps [valg]",
        help="Finn filer der ExifTool ser metadata-dato som bildebank ikke leser",
    )
    exiftool_gaps.add_argument(
        "--exiftool",
        type=Path,
        help="Path til exiftool.exe. Standard er Bildebanks managed ExifTool, ellers exiftool fra PATH.",
    )
    exiftool_gaps.add_argument(
        "--batch-size",
        type=positive_int_arg,
        default=DEFAULT_EXIFTOOL_BATCH_SIZE,
        help=f"Antall filer per ExifTool-kall. Standard: {DEFAULT_EXIFTOOL_BATCH_SIZE}",
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
    geo_scan.add_argument(
        "--override-manual-h3",
        action="store_true",
        help=(
            "Ta med filer med manuell H3-lokasjon. "
            "Den manuelle H3-lokasjonen overskrives av GPS fra metadata, "
            "eller slettes hvis filen ikke har GPS."
        ),
    )
    geo_scan.add_argument("--limit", type=positive_int_arg, help="Maks antall filer som skal scannes")
    geo_scan.add_argument("--verbose", action="store_true", help="Vis filer uten GPS eller med feil")
    geo_scan.add_argument(
        "--exiftool",
        type=Path,
        help="Path til exiftool. Standard er Bildebanks managed ExifTool, ellers exiftool fra PATH.",
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
    geo_areas.add_argument("--resolution", type=h3_resolution_arg, default=7, help="H3-oppløsning 0-11. Standard: 7")
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
        "--rescan",
        action="store_true",
        help="Les metadata på nytt for alle aktive filer",
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
        description="Lag thumbnails for månedsvisning",
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
        description="Vis hvor Bildebank og kjente bildesamlinger ligger",
    )
    backup = add_command(
        subparsers,
        "backup",
        usage="bildebank backup [valg] plassering",
        help="Lag eller oppdater backup av bildesamlingen",
        description=(
            "Lag eller oppdater backup av bildesamlingen. "
            "NB: les dokumentasjonen for denne kommandoen før du betror alle "
            "bildene dine til bildebank."
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
        "doctor",
        usage="bildebank doctor [valg]",
        help="Vis diagnose for installasjon og aktiv bildesamling",
    )
    add_command(
        subparsers,
        "face-status",
        usage="bildebank face-status [valg]",
        help="Gammelt navn for doctor",
    )
    config_parser = add_command(
        subparsers,
        "config",
        usage="bildebank config seksjon enable|disable",
        help="Slå valgfrie funksjoner på eller av",
        description="Slå valgfrie funksjoner på eller av i bildebank-config.toml.\n"
                    "Eksempel:\n\n"
                    " bildebank config face_recognition enable\n"
                    " bildebank config image_search disable",
        formatter_class=BildebankHelpFormatter,
    )
    config_parser.add_argument(
        "section",
        metavar="seksjon",
        choices=("face_recognition", "image_search"),
        help="Config-seksjon som skal endres",
    )
    config_parser.add_argument(
        "action",
        metavar="enable|disable",
        choices=("enable", "disable"),
        help="enable slår funksjonen på, disable slår den av",
    )
    face_config = add_command(
        subparsers,
        "face-config",
        usage="bildebank face-config true|false",
        help="Slå ansiktsgjenkjenning på eller av",
        description="Slå ansiktsgjenkjenning på eller av",
    )
    face_config.add_argument(
        "enabled",
        metavar="true|false",
        type=bool_arg,
        help="true slår på ansiktsgjenkjenning, false slår den av",
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
        description="Start Bildebank-server som lar deg se bildene i nettleser."
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
        "--force",
        action="store_true",
        help="Scan valgte bilder på nytt selv om de allerede er scannet",
    )
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
        description="Slett person fra ansiktsdatabasen",
    )
    face_person_delete.add_argument("name", metavar="navn", help="Personnavn")
    face_person_rename = add_command(
        subparsers,
        "face-person-rename",
        usage="bildebank face-person-rename [valg] gammelt_navn nytt_navn",
        help="Endre navn på person i ansiktsdatabasen",
        description="Endre navn på person i ansiktsdatabasen",
    )
    face_person_rename.add_argument("old_name", metavar="gammelt_navn", help="Eksisterende personnavn")
    face_person_rename.add_argument("new_name", metavar="nytt_navn", help="Nytt personnavn")
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
    face_suggest.add_argument(
        "--model",
        metavar="NAVN",
        help="Bruk face-database for denne InsightFace-modellen uten å endre config-filen",
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
        description="Lag HTML-index og personside for alle registrerte personer",
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
        help="Oppgrader databasen etter programoppdatering",
        description="Validerer og oppgraderer databasen etter en programoppdatering.",
    )
    migrate.add_argument(
        "--check",
        action="store_true",
        help="Vis hva migreringen vil gjøre uten å endre databasen",
    )
    add_command(
        subparsers,
        "vacuum",
        usage="bildebank vacuum [valg]",
        help="Pakk databasen så SQLite-filen krymper fysisk",
        description="Kjører SQLite VACUUM på Bildebank-databasen. Kommandoen endrer ikke bildefiler.",
    )
    add_command(subparsers, "update", usage="bildebank update [valg]", help="Oppdater programinstallasjonen",
                description="Oppdater Bildebank til siste versjon fra GitHub.")
    exiftool_install = add_command(
        subparsers,
        "exiftool-install",
        usage="bildebank exiftool-install [valg]",
        help="Installer ExifTool for GPS og metadata",
        description="Last ned og installer ExifTool i programmappen.",
    )
    exiftool_install.add_argument(
        "--force",
        action="store_true",
        help="Installer ExifTool på nytt selv om den allerede finnes.",
    )

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
    formatter_class: type[argparse.HelpFormatter] | None = None,
) -> argparse.ArgumentParser:
    kwargs = {"formatter_class": formatter_class} if formatter_class is not None else {}
    return subparsers.add_parser(name, help=help, description=description, usage=usage, **kwargs)


def bool_arg(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise argparse.ArgumentTypeError("Bruk true eller false.")


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
    lines.append("Vanlig start:")
    lines.append("   bildebank create \"C:\\Users\\Tom\\Bilder samlet\"")
    lines.append("   cd \"C:\\Users\\Tom\\Bilder samlet\"")
    lines.append("   bildebank import --name \"Mobil 2024\" --dry-run \"E:\\DCIM\"")
    lines.append("   bildebank import --name \"Mobil 2024\" \"E:\\DCIM\"")
    lines.append("   bildebank run-server")
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

    if args.command == "exiftool-install":
        return run_exiftool_install(force=args.force)

    if args.command == "where-is":
        return run_where_is()

    if args.command in {"doctor", "face-status"}:
        return run_doctor(args.target)

    if args.command == "config":
        return run_config(args.section, enabled=args.action == "enable")

    if args.command == "face-config":
        return run_face_config(args.enabled)

    target = resolve_target(args.target)
    record_target_best_effort(program_repo_root(), target)
    if args.command == "migrate":
        return run_migrate(target, check=args.check)

    if args.command == "backup":
        return run_backup_command(target, args.destination, dry_run=args.dry_run)

    if args.command == "vacuum":
        return run_vacuum(target)

    if args.command == "geo-scan":
        return run_geo_scan(
            target,
            force=args.force,
            only_missing=args.only_missing,
            override_manual_h3=args.override_manual_h3,
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
        "face-person-rename",
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
        return run_face_scan(
            target,
            limit=args.limit,
            force=args.force,
            show_model_output=args.show_model_output,
        )

    if args.command == "face-report":
        return run_face_report(target, limit=args.limit)

    if args.command == "face-person-create":
        config = load_config(program_repo_root()).face_recognition
        person_id = create_person(target, args.name, config)
        print(f"Person #{person_id}: {args.name.strip()}")
        return 0

    if args.command == "face-person-add-face":
        config = load_config(program_repo_root()).face_recognition
        result = add_face_to_person(target, args.name, args.face_id, config)
        print_add_face_to_person_result(result)
        return 0

    if args.command == "face-person-remove-face":
        config = load_config(program_repo_root()).face_recognition
        result = remove_face_from_person(target, args.name, args.face_id, config)
        print_remove_face_from_person_result(result)
        return 0

    if args.command == "face-person-delete":
        return run_face_person_delete(target, args.name)

    if args.command == "face-person-rename":
        config = load_config(program_repo_root()).face_recognition
        result = rename_person(target, args.old_name, args.new_name, config)
        print_rename_person_result(result)
        return 0

    if args.command == "face-person-list":
        print_persons(target)
        return 0

    if args.command == "face-suggest":
        return run_face_suggest(target, threshold=args.threshold, model=args.model)

    if args.command == "make-face-browser":
        config = load_config(program_repo_root()).face_recognition
        output = args.output.resolve() if args.output else None
        output_path = export_face_browser(target, output, limit=args.limit, config=config)
        print(f"Skrev HTML-browser for ansikter: {output_path}")
        return 0

    if args.command == "make-person-browser":
        output = args.output.resolve() if args.output else None
        output_path = export_person_browser(
            target,
            args.name,
            output,
            month_preview_limit=args.month_preview_limit,
            config=load_config(program_repo_root()).face_recognition,
        )
        print(f"Skrev HTML-browser for person: {output_path}")
        return 0

    if args.command == "make-people-browser":
        result = export_people_browser(
            target,
            month_preview_limit=args.month_preview_limit,
            config=load_config(program_repo_root()).face_recognition,
        )
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

    if args.command == "check-source":
        return run_check_source(target, args.path, verbose=not args.quiet)

    conn = db.connect(target)
    try:
        if not (args.command == "unimport" and args.dry_run):
            db.log_command(conn, args.command, vars_for_log(args))
        if args.command == "import":
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
                validate_unimport_source_files(conn, source, progress=unimport_source_progress())
                validate_unimport_target_files(plan, progress=unimport_target_progress())
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
                print(f"{db.absolute_target_path(target, Path(str(row['target_path'])))}")
                print(f"  dato: {row['date_source']}\t{taken_date}")
                if args.with_source:
                    print(f"  kilde: {row['source_path']}")
            return 0

        if args.command == "date-set":
            row = resolve_db_file_for_tag_command(conn, target, args.path)
            date_from, date_to = manual_date_range_from_args(args)
            db.set_manual_date(
                conn,
                file_id=int(row["id"]),
                date_from=date_from.isoformat(),
                date_to=date_to.isoformat(),
                note=args.note,
            )
            conn.commit()
            print(f"Manuell dato satt: {manual_date_range_text(date_from, date_to)}")
            print(f"Fil: {db.absolute_target_path(target, Path(str(row['target_path'])))}")
            if args.note:
                print(f"Notat: {db.clean_manual_date_note(args.note)}")
            return 0

        if args.command == "date-clear":
            row = resolve_db_file_for_tag_command(conn, target, args.path)
            db.clear_manual_date(conn, file_id=int(row["id"]))
            conn.commit()
            print(f"Manuell dato fjernet: {db.absolute_target_path(target, Path(str(row['target_path'])))}")
            return 0

        if args.command == "tag-list":
            if args.path is None:
                for row in db.tags(conn):
                    print(f"{row['name']}\t{row['file_count']}\t{row['kind']}")
                return 0
            row = resolve_db_file_for_tag_command(conn, target, args.path)
            for tag in db.tags_for_file(conn, int(row["id"])):
                print(f"{tag['name']}\t{tag['kind']}")
            return 0

        if args.command == "tag-add":
            row = resolve_db_file_for_tag_command(conn, target, args.path)
            added = db.tag_file(conn, file_id=int(row["id"]), tag_name=args.tag)
            conn.commit()
            action = "La til" if added else "Fantes allerede"
            print(f"{action}: {db.normalize_tag_name(args.tag)} -> {db.absolute_target_path(target, Path(str(row['target_path'])))}")
            return 0

        if args.command == "tag-remove":
            row = resolve_db_file_for_tag_command(conn, target, args.path)
            removed = db.untag_file(conn, file_id=int(row["id"]), tag_name=args.tag)
            conn.commit()
            action = "Fjernet" if removed else "Fant ikke"
            print(f"{action}: {db.normalize_tag_name(args.tag)} -> {db.absolute_target_path(target, Path(str(row['target_path'])))}")
            return 0

        if args.command == "tag-files":
            for row in db.tagged_files(conn, args.tag):
                taken_date = row["taken_date"] or "-"
                print(f"{taken_date}\t{db.absolute_target_path(target, Path(str(row['target_path'])))}")
            return 0

        if args.command == "exiftool-metadata-gaps":
            exiftool_path = args.exiftool.resolve() if args.exiftool else None
            conn.commit()
            conn.close()
            gaps = exiftool_metadata_gaps(
                target,
                exiftool_path=exiftool_path,
                batch_size=args.batch_size,
                progress=True,
                repo_root=program_repo_root(),
            )
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
                target,
                dry_run=args.dry_run,
                rescan=args.rescan,
                verbose=args.verbose,
                progress=print_refresh_metadata_progress,
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
            print("report er slått sammen med status")
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
    global THUMBNAIL_PROGRESS
    if stage == "start":
        THUMBNAIL_PROGRESS = ProgressMeter("Thumbnails")
        THUMBNAIL_PROGRESS.message(f"Thumbnails: {total} filer skal kontrolleres.")
        return
    if THUMBNAIL_PROGRESS is None:
        THUMBNAIL_PROGRESS = ProgressMeter("Thumbnails")
    if stage == "error":
        message = stats.last_error_message or "ukjent feil"
        THUMBNAIL_PROGRESS.error(f"Thumbnail-feil: {path}\t{message}")
        return
    if stage == "check":
        THUMBNAIL_PROGRESS.update(
            current,
            total,
            action="kontrollert",
            details=(
                f"sjekket={stats.checked}, laget={stats.created}, "
                f"ferske={stats.skipped_current}, feil={stats.errors}"
            ),
            eta=True,
        )
        return
    if stage == "done":
        THUMBNAIL_PROGRESS.done(f"Thumbnails: ferdig kontrollert {min(current, total)}/{total} filer.")
        THUMBNAIL_PROGRESS = None
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


def validate_unimport_source_files(
    conn,
    source: db.Source,
    *,
    progress: ProgressMeter | None = None,
) -> None:
    rows = db.source_file_sources(conn, source.id)
    total = len(rows)
    if progress is not None:
        progress.message(f"Unimport: kontrollerer {total} kildefiler.")
        if total == 0:
            progress.update(0, 0, action="kildefiler", eta=True)
    try:
        for index, row in enumerate(rows, start=1):
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
            if progress is not None:
                progress.update(index, total, action="kildefiler", eta=True)
    finally:
        if progress is not None:
            progress.done()


def validate_unimport_target_files(
    plan: db.UnimportPlan,
    *,
    progress: ProgressMeter | None = None,
) -> None:
    total = len(plan.target_paths_to_delete)
    if progress is not None:
        progress.message(f"Unimport: kontrollerer {total} målfil(er) som kan fjernes.")
        if total == 0:
            progress.update(0, 0, action="målfiler", eta=True)
    try:
        for index, target_path in enumerate(plan.target_paths_to_delete, start=1):
            if not target_path.exists():
                raise ValueError(f"Målfilen som skulle fjernes finnes ikke: {target_path}")
            if not target_path.is_file():
                raise ValueError(f"Målfilen som skulle fjernes er ikke en fil: {target_path}")
            if progress is not None:
                progress.update(index, total, action="målfiler", eta=True)
    finally:
        if progress is not None:
            progress.done()


def unimport_source_progress() -> ProgressMeter:
    global UNIMPORT_SOURCE_PROGRESS
    UNIMPORT_SOURCE_PROGRESS = ProgressMeter("Unimport")
    return UNIMPORT_SOURCE_PROGRESS


def unimport_target_progress() -> ProgressMeter:
    global UNIMPORT_TARGET_PROGRESS
    UNIMPORT_TARGET_PROGRESS = ProgressMeter("Unimport")
    return UNIMPORT_TARGET_PROGRESS


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


def run_exiftool_install(*, force: bool = False) -> int:
    if sys.platform != "win32":
        raise ValueError(
            "exiftool-install støttes bare på Windows. "
            "Installer ExifTool med pakkesystemet, for eksempel: sudo apt install libimage-exiftool-perl"
        )
    result = install_managed_exiftool(program_repo_root(), force=force)
    if result.installed:
        print(f"Installerte ExifTool {result.version}: {result.path}")
    else:
        print(f"ExifTool er allerede installert ({result.version}): {result.path}")
    return 0


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
    override_manual_h3: bool,
    limit: int | None,
    verbose: bool,
    exiftool_path: Path | None,
    batch_size: int,
) -> int:
    if force and only_missing:
        raise ValueError("--force og --only-missing kan ikke brukes samtidig.")
    if override_manual_h3 and only_missing:
        raise ValueError("--override-manual-h3 og --only-missing kan ikke brukes samtidig.")
    stats = scan_geo(
        target,
        force=force,
        only_missing=only_missing,
        override_manual_h3=override_manual_h3,
        limit=limit,
        verbose=verbose,
        exiftool_path=exiftool_path.resolve() if exiftool_path else None,
        batch_size=batch_size,
        repo_root=program_repo_root(),
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


def run_vacuum(target: Path) -> int:
    database_path = db.db_path_for_target(target)
    before = database_path.stat().st_size
    conn = db.connect(target)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
    after = database_path.stat().st_size
    print(f"Database: {database_path}")
    print(f"Størrelse før:  {format_bytes(before)}")
    print(f"Størrelse etter: {format_bytes(after)}")
    print("Ferdig. Databasen er pakket.")
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


def run_doctor(target_arg: Path | None = None) -> int:
    repo_root = program_repo_root()
    config_path = repo_root / CONFIG_FILENAME
    config = load_config(repo_root)
    face = config.face_recognition

    print("Bildebank doctor")
    print()
    if config_path.exists():
        doctor_ok(f"config-fil funnet: {config_path}")
    else:
        doctor_obs("config-fil ikke funnet. Bildebank bruker standardvalg.")
        doctor_advice("Kjør `bildebank config ...` hvis du vil slå på valgfrie funksjoner.")

    if python_module_available("h3"):
        doctor_ok("h3 installert")
    else:
        doctor_error("h3 mangler. Geografiske funksjoner virker ikke.")
        doctor_advice("Kjør setup-windows.ps1 på nytt, eller installer Bildebank på nytt.")

    try:
        exiftool_path = resolve_exiftool_path(repo_root)
        exiftool_version = validate_exiftool_install(exiftool_path)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        doctor_error(f"ExifTool mangler eller virker ikke: {exc}")
        if sys.platform == "win32":
            doctor_advice("Kjør `bildebank exiftool-install` fra programmappen.")
        else:
            doctor_advice("Installer ExifTool med pakkesystemet, for eksempel `sudo apt install libimage-exiftool-perl`.")
    else:
        doctor_ok(f"ExifTool funnet: {exiftool_path} ({exiftool_version})")

    print()
    print("Ansiktsgjenkjenning:")
    if face.enabled:
        doctor_ok(f"face_recognition er slått på ({face.model_name}, {face.provider})")
        if python_module_available("insightface"):
            doctor_ok("insightface installert")
        else:
            doctor_error("face_recognition er slått på, men insightface mangler.")
            doctor_advice("Kjør `.\\install-insightface.ps1` fra programmappen.")
        if python_module_available("onnxruntime"):
            doctor_ok("onnxruntime installert")
        else:
            doctor_error("face_recognition er slått på, men onnxruntime mangler.")
            doctor_advice("Kjør `.\\install-insightface.ps1` fra programmappen.")
    else:
        doctor_obs("face_recognition er slått av.")

    print()
    print("Tekstbasert bildesøk:")
    if config.openclip.enabled:
        doctor_ok(f"image_search er slått på ({config.openclip.model_name}, {config.openclip.device})")
        if python_module_available("open_clip"):
            doctor_ok("open_clip installert")
        else:
            doctor_error("image_search er slått på, men open_clip mangler.")
            doctor_advice("Kjør `.\\install-openclip.ps1` fra programmappen.")
        gpu_status = torch_gpu_status()
        if gpu_status["torch"] == "ja":
            doctor_ok("torch installert")
            if gpu_status["cuda"] == "ja":
                doctor_ok(f"CUDA/GPU funnet: {gpu_status['device']}")
            elif config.openclip.device != "cpu":
                doctor_obs("CUDA/GPU ikke funnet. Bildesøk kan bruke CPU, men blir tregere.")
        else:
            doctor_error("image_search er slått på, men torch mangler.")
            doctor_advice("Kjør `.\\install-openclip.ps1` fra programmappen.")
    else:
        doctor_obs("image_search er slått av.")

    target = db.find_target(target_arg)
    if target is not None:
        exists, scanned, faces = face_db_summary(target, face)
        openclip_summary = openclip_db_summary(target)
        print()
        print("Aktiv bildesamling:")
        doctor_ok(f"aktiv bildesamling funnet: {target}")
        if exists:
            doctor_ok(f"face-database finnes: {face_db_path(target, face)}")
            doctor_info(f"scannede filer: {scanned}")
            doctor_info(f"ansikter funnet: {faces}")
        else:
            doctor_obs(f"face-database finnes ikke ennå: {face_db_path(target, face)}")
        if openclip_summary.exists:
            doctor_ok(f"openclip-database finnes: {openclip_db_path(target)}")
            doctor_info(f"bilde-embeddings: {openclip_summary.embeddings}")
            doctor_info(f"bildesøk: {openclip_summary.search_runs}")
        else:
            doctor_obs(f"openclip-database finnes ikke ennå: {openclip_db_path(target)}")
    else:
        print()
        print("Aktiv bildesamling:")
        doctor_obs("ingen aktiv bildesamling funnet.")
        doctor_advice("Kjør kommandoen fra en bildesamling, eller bruk `--target`.")
        doctor_advice('Eksempel: "bildebank --target C:\\bildesamling doctor"')
    return 0


def run_face_config(enabled: bool) -> int:
    print(f"Ansiktsgjenkjenning er satt til {'på' if enabled else 'av'}.")
    return run_config("face_recognition", enabled=enabled)


def run_config(section: str, *, enabled: bool) -> int:
    repo_root = program_repo_root()
    config_path = set_config_enabled(repo_root, section, enabled)
    print(f"{section}.enabled er satt til {_toml_bool_text(enabled)}.")
    print(f"Config-fil: {config_path}")
    return 0


def _toml_bool_text(value: bool) -> str:
    return "true" if value else "false"


def doctor_ok(message: str) -> None:
    print(f"  OK: {message}")


def doctor_obs(message: str) -> None:
    print(f"  OBS: {message}")


def doctor_error(message: str) -> None:
    print(f"  FEIL: {message}")


def doctor_advice(message: str) -> None:
    print(f"  Råd: {message}")


def doctor_info(message: str) -> None:
    print(f"  INFO: {message}")


def python_module_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def module_available(module_name: str) -> str:
    return "ja" if python_module_available(module_name) else "nei"


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
    global IMAGE_SCAN_PROGRESS
    if stage == "start":
        IMAGE_SCAN_PROGRESS = ProgressMeter("Image-scan")
        IMAGE_SCAN_PROGRESS.message(f"Image-scan: {total} bildefiler skal kontrolleres.")
        return
    if IMAGE_SCAN_PROGRESS is None:
        IMAGE_SCAN_PROGRESS = ProgressMeter("Image-scan")
    if stage == "load_model":
        IMAGE_SCAN_PROGRESS.reset_eta()
        IMAGE_SCAN_PROGRESS.message(f"Image-scan: {stats.to_scan} nye eller endrede bilder skal scannes.")
        IMAGE_SCAN_PROGRESS.message("Image-scan: laster OpenCLIP-modell. Det kan ta litt tid.")
        return
    if stage == "error":
        message = getattr(stats, "last_error_message", None) or "ukjent feil"
        IMAGE_SCAN_PROGRESS.error(f"Image-scan-feil: {path}\t{message}")
        return
    if stage == "check":
        IMAGE_SCAN_PROGRESS.update(
            current,
            total,
            action="kontrollert",
            details=f"hoppet_over={stats.skipped}, skal_scannes={stats.to_scan}",
            eta=True,
        )
        return
    if stage == "scan":
        IMAGE_SCAN_PROGRESS.update(
            current,
            total,
            action="scannet",
            details=(
                f"behandlet={stats.skipped + current}/{stats.total}, "
                f"hoppet_over={stats.skipped}, feil={stats.errors}"
            ),
            eta=True,
        )
        return
    if stage == "done":
        IMAGE_SCAN_PROGRESS.done()
        IMAGE_SCAN_PROGRESS = None
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
    global IMAGE_SEARCH_PROGRESS
    if stage == "load_model":
        IMAGE_SEARCH_PROGRESS = ProgressMeter("Image-search")
        IMAGE_SEARCH_PROGRESS.message(f"Image-search: fant {total} bilde-embeddings. Laster OpenCLIP-modell.")
        return
    if IMAGE_SEARCH_PROGRESS is None:
        IMAGE_SEARCH_PROGRESS = ProgressMeter("Image-search")
    if stage == "compare_start":
        IMAGE_SEARCH_PROGRESS.reset_eta()
        IMAGE_SEARCH_PROGRESS.message(f'Image-search: søker etter "{stats.query}" i {total} bilder.')
        return
    if stage == "compare":
        IMAGE_SEARCH_PROGRESS.update(current, total, action="søkt", eta=True)
        return
    if stage == "write":
        IMAGE_SEARCH_PROGRESS.message(f"Image-search: skriver {current} treff til image-search.html.")
        return
    if stage == "done":
        IMAGE_SEARCH_PROGRESS.done()
        IMAGE_SEARCH_PROGRESS = None
        return


def run_face_scan(
    target: Path,
    *,
    limit: int | None,
    force: bool = False,
    show_model_output: bool = False,
) -> int:
    config = load_config(program_repo_root()).face_recognition
    require_face_enabled(config.enabled)
    stats = scan_faces(
        target,
        config,
        limit=limit,
        progress=print_face_scan_progress,
        show_model_output=show_model_output,
        force=force,
    )
    print(
        "Oppsummering: "
        f"sjekket={stats.checked}, hoppet_over={stats.skipped}, "
        f"scannet={stats.scanned}, ansikter={stats.faces}, feil={stats.errors}"
    )
    print(f"Face-database: {face_db_path(target, config)}")
    return 0 if stats.errors == 0 else 2


def print_face_scan_progress(
    stage: str,
    current: int,
    total: int,
    stats,
    path: Path | None,
) -> None:
    global FACE_SCAN_PROGRESS
    if stage == "start":
        FACE_SCAN_PROGRESS = ProgressMeter("Face-scan")
        FACE_SCAN_PROGRESS.message(f"Face-scan: {total} bildefiler skal kontrolleres.")
        return
    if FACE_SCAN_PROGRESS is None:
        FACE_SCAN_PROGRESS = ProgressMeter("Face-scan")
    if stage == "check":
        FACE_SCAN_PROGRESS.update(
            current,
            total,
            action="kontrollert",
            details=f"hoppet_over={stats.skipped}, skal_scannes={stats.checked - stats.skipped}",
            eta=True,
        )
        return
    if stage == "load_model":
        FACE_SCAN_PROGRESS.reset_eta()
        FACE_SCAN_PROGRESS.message(f"Face-scan: {total} nye eller endrede bilder skal scannes.")
        FACE_SCAN_PROGRESS.message("Face-scan: laster ansiktsmodell. Det kan ta 20 sekunder eller mer.")
        return
    if stage == "download_model":
        FACE_SCAN_PROGRESS.message(
            "Face-scan: ansiktsmodellen finnes ikke lokalt. InsightFace vil laste den ned nå\n"
            "(ca 700 MB), så første kjøring kan ta ekstra lang tid"
        )
        return
    if stage == "error":
        message = getattr(stats, "last_error_message", None) or "ukjent feil"
        FACE_SCAN_PROGRESS.error(f"Face-scan-feil: {path}\t{message}")
        return
    if stage == "scan":
        FACE_SCAN_PROGRESS.update(
            current,
            total,
            action="scannet",
            details=f"ansikter={stats.faces}, feil={stats.errors}",
            eta=True,
        )
        return
    if stage == "done":
        FACE_SCAN_PROGRESS.done()
        FACE_SCAN_PROGRESS = None
        return


def run_face_report(target: Path, *, limit: int) -> int:
    config = load_config(program_repo_root()).face_recognition
    report = face_report(target, limit=limit, config=config)
    print_face_report(target, report, config=config)
    return 0


def run_face_suggest(target: Path, *, threshold: float, model: str | None = None) -> int:
    config = load_config(program_repo_root()).face_recognition
    if model is not None:
        model_name = model.strip()
        if not model_name:
            raise ValueError("Modellnavn kan ikke være tomt.")
        config = replace(config, model_name=model_name)
    stats = suggest_faces(target, threshold=threshold, config=config, progress=print_face_suggest_progress)
    print_face_suggest_stats(stats)
    print(f"Modell: {config.model_name}")
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
    config = load_config(program_repo_root()).face_recognition
    result = delete_person(target, clean_name, config)
    print_delete_person_result(result)
    return 0


def print_delete_person_result(result: DeletePersonResult) -> None:
    print(f"Slettet person: {result.person_name}")
    print(f"Fjernet bekreftede ansiktskoblinger: {result.removed_faces}")
    print(f"Fjernet ansiktsforslag: {result.removed_suggestions}")
    print("Ingen bilder eller scannede ansikter er slettet.")


def print_rename_person_result(result: RenamePersonResult) -> None:
    if result.old_name == result.new_name:
        print(f"Personnavnet er uendret: {result.new_name}")
        return
    print(f"Endret personnavn: {result.old_name} -> {result.new_name}")


def print_face_suggest_stats(stats: FaceSuggestStats) -> None:
    print(
        "Ansiktsforslag: "
        f"personer={stats.persons}, ukjente_ansikter={stats.unknown_faces}, "
        f"forslag={stats.suggestions}, threshold={stats.threshold:.3f}"
    )


def print_face_suggest_progress(
    stage: str,
    current: int,
    total: int,
    stats,
    path: Path | None,
) -> None:
    global FACE_SUGGEST_PROGRESS
    if stage == "load_known_start":
        FACE_SUGGEST_PROGRESS = ProgressMeter("Face-suggest")
        FACE_SUGGEST_PROGRESS.message(f"Face-suggest: leser {total} bekreftede ansikter.")
        return
    if FACE_SUGGEST_PROGRESS is None:
        FACE_SUGGEST_PROGRESS = ProgressMeter("Face-suggest")
    if stage == "load_known":
        FACE_SUGGEST_PROGRESS.update(
            current,
            total,
            action="bekreftede_ansikter",
            details=f"personer={stats.persons}",
            eta=True,
        )
        return
    if stage == "load_unknown_start":
        FACE_SUGGEST_PROGRESS.reset_eta()
        FACE_SUGGEST_PROGRESS.message(f"Face-suggest: leser {total} ukjente ansikter.")
        return
    if stage == "load_unknown":
        FACE_SUGGEST_PROGRESS.update(
            current,
            total,
            action="ukjente_ansikter",
            details=f"personer={stats.persons}",
            eta=True,
        )
        return
    if stage == "compare_start":
        FACE_SUGGEST_PROGRESS.reset_eta()
        FACE_SUGGEST_PROGRESS.message(
            f"Face-suggest: sammenligner {total} ukjente ansikter mot {stats.persons} personer."
        )
        return
    if stage == "compare":
        FACE_SUGGEST_PROGRESS.update(
            current,
            total,
            action="sammenlignet",
            details=f"forslag={stats.suggestions}, threshold={stats.threshold:.3f}",
            eta=True,
        )
        return
    if stage == "done":
        FACE_SUGGEST_PROGRESS.done()
        FACE_SUGGEST_PROGRESS = None
        return


def print_persons(target: Path) -> None:
    config = load_config(program_repo_root()).face_recognition
    rows = list_persons(target, config)
    if not rows:
        print("Ingen personer registrert.")
        return
    name_width = max(len("Navn"), *(len(str(row["name"])) for row in rows))
    print(
        f"{'Navn':<{name_width}}  "
        f"{'Bilder':>6}  "
        f"{'Ansikter':>8}  "
        f"{'Forslag':>7}  "
        "Oppdatert"
    )
    print(
        f"{'-' * name_width}  "
        f"{'-' * 6}  "
        f"{'-' * 8}  "
        f"{'-' * 7}  "
        f"{'-' * 19}"
    )
    for row in rows:
        print(
            f"{str(row['name']):<{name_width}}  "
            f"{int(row['confirmed_file_count']):>6}  "
            f"{int(row['face_count']):>8}  "
            f"{int(row['suggestion_count']):>7}  "
            f"{row['updated_at']}"
        )


def print_face_report(target: Path, report: FaceReport, *, config=None) -> None:
    print("Ansiktsrapport")
    print(f"  Bildesamling: {target}")
    print(f"  Face-database: {face_db_path(target, config)}")
    if not report.database_exists:
        print("  Face-database finnes ikke.")
        print("  Kjør bildebank face-scan først.")
        return
    print(f"  Scannede filer: {report.scanned_files}")
    print(f"  Ansikter funnet: {report.total_faces}")
    print(f"  Filer uten ansikter: {report.files_with_zero_faces}")
    print(f"  Filer med ett ansikt: {report.files_with_one_face}")
    print(f"  Filer med flere ansikter: {report.files_with_multiple_faces}")
    print(f"  Scan-feil: {report.scan_errors}")
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
    config = load_config(program_repo_root()).face_recognition
    path = face_db_path(target, config)
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
    result = reset_face_database(target, mode=mode, config=config)
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
            f"Tekstbasert bildesøk er av. Kjør `bildebank config image_search enable` "
            f"eller sett enabled = true under [image_search] i {CONFIG_FILENAME} hvis du vil teste."
        )


def run_named_import_dry_run(target: Path, args: argparse.Namespace) -> int:
    if not args.name or args.path is None:
        raise ValueError('Bruk både --name og mappe: bildebank import --name "Navn" "path\\til\\kilde"')
    source = existing_path_arg(args.path).resolve()
    if not source.is_dir():
        raise ValueError(f"Kilden finnes ikke som mappe: {source}")
    validate_source_target(source, target)
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
        stats = import_source_dry_run(
            conn, target, source_row, output=sys.stdout, verbose=not args.quiet
        )
        print_summary(stats)
        return 0 if stats.errors == 0 else 2
    finally:
        conn.close()


@dataclass
class CheckSourceStats:
    scanned: int = 0
    covered: int = 0
    missing: int = 0
    source_errors: int = 0
    target_errors: int = 0


@dataclass(frozen=True)
class CheckSourceProblem:
    path: Path
    reason: str


def run_check_source(target: Path, source_arg: Path, *, verbose: bool = True) -> int:
    source = existing_path_arg(source_arg).resolve()
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
            progress.message(f"Check-source: scanner {source}.")
        for item in iter_check_source_files(source):
            if isinstance(item, WalkError):
                stats.source_errors += 1
                problems.append(CheckSourceProblem(item.path, item.message))
                continue
            stats.scanned += 1
            path = item
            try:
                file_hash = sha256_file(path)
            except OSError as exc:
                stats.source_errors += 1
                problems.append(CheckSourceProblem(path, f"kan ikke lese kildefil: {exc}"))
                continue

            rows = db.active_files_by_hash(conn, file_hash)
            if not rows:
                stats.missing += 1
                problems.append(
                    CheckSourceProblem(path, "filen er ikke importert i bildesamlingen med samme SHA-256")
                )
            elif check_source_hash_is_validated(target, rows, target_hash_cache):
                stats.covered += 1
            else:
                stats.target_errors += 1
                problems.append(CheckSourceProblem(path, "matchende målfil mangler eller har endret innhold"))

            if progress is not None:
                progress.update_count(
                    stats.scanned,
                    action="kontrollert",
                    details=check_source_progress_details(stats),
                )
        if progress is not None:
            progress.done()
    finally:
        conn.close()

    print_check_source_report(source, stats, problems)
    return 0 if check_source_is_safe(stats) else 2


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
        f"dekket={stats.covered}, mangler={stats.missing}, "
        f"kildefeil={stats.source_errors}, målfeil={stats.target_errors}"
    )


def check_source_is_safe(stats: CheckSourceStats) -> bool:
    return stats.missing == 0 and stats.source_errors == 0 and stats.target_errors == 0


def print_check_source_report(source: Path, stats: CheckSourceStats, problems: list[CheckSourceProblem]) -> None:
    print("Check-source")
    print(f"  Kildemappe: {source}")
    print(
        "  Oppsummering: "
        f"scannet={stats.scanned}, dekket={stats.covered}, mangler={stats.missing}, "
        f"kildefeil={stats.source_errors}, målfeil={stats.target_errors}"
    )
    if problems:
        print("  Det finnes filer som ikke er importert i bildesamlingen, eller som ikke kan valideres.")
        print("  Kildemappen er derfor ikke trygg å slette.")
        print("Problemer:")
        for problem in problems:
            print(f"- {problem.path}")
            print(f"  {problem.reason}")
        return

    print("  Alle filer i kildemappen finnes i bildesamlingen og er validert med SHA-256.")
    print("  Bildebank sletter ikke kildemapper.")
    print("  Hvis du vil slette mappen selv i PowerShell:")
    print()
    print(f"  Remove-Item -LiteralPath {powershell_literal(str(source))}")
    print()
    print("  Hvis mappen inneholder filer, spør PowerShell før den sletter.")


def powershell_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def run_migrate(target: Path, *, check: bool) -> int:
    plan = db.migration_plan(target, validate=check)
    print(f"Bildesamling: {target}")
    print(f"Database: {db.db_path_for_target(target)}")
    print(f"Nåværende schema_version: {plan.current_version}")
    print(f"Ny schema_version: {plan.target_version}")
    if plan.current_version == plan.target_version and not plan.refreshes_performance_indexes:
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
    if plan.cleans_gps_errors:
        print("  rydde gamle GPS-feilmeldinger")
    if plan.backfills_h3_10_11:
        print("  fylle h3_res10 og h3_res11 for eksisterende GPS-posisjoner")
    if plan.adds_camera_columns:
        print("  legge til kamerakolonner i files")
    if plan.refreshes_performance_indexes:
        print("  oppdatere manglende ytelsesindekser")
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
    if result.cleans_gps_errors:
        print("Rydder gamle GPS-feilmeldinger.")
    if result.backfills_h3_10_11:
        print("Fyller h3_res10 og h3_res11 for eksisterende GPS-posisjoner.")
    if result.adds_camera_columns:
        print("Legger til kamerakolonner i files.")
        print("Kjør bildebank refresh-metadata --rescan for å fylle kameradata for eksisterende filer.")
    if result.refreshes_performance_indexes:
        print("Oppdaterer manglende ytelsesindekser.")
    print(f"Setter schema_version={result.target_version}.")
    print("Ferdig. Databasen er migrert.")
    if result.cleans_gps_errors:
        print("Kjør bildebank vacuum hvis du vil krympe SQLite-filen fysisk.")
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


def resolve_db_file_for_tag_command(conn, target: Path, path: Path):
    resolved = resolve_target_file_arg(target, path)
    row = db.file_by_target_path(conn, target, resolved)
    if row is None:
        raise ValueError(f"Filen finnes ikke i importdatabasen: {resolved}")
    if row["deleted_at"] is not None:
        raise ValueError(f"Filen er markert som slettet: {resolved}")
    return row


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
    manual_date = manual_date_from_row(row)
    if manual_date:
        print(f"Manuell dato: {manual_date}")
        if row["manual_date_note"]:
            print(f"Datonotat: {row['manual_date_note']}")
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
    manual_date = manual_date_from_row(first)
    if manual_date:
        print(f"Manuell dato: {manual_date}")
        if first["manual_date_note"]:
            print(f"Datonotat: {first['manual_date_note']}")
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


def iso_date_arg(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("må være dato på formen YYYY-MM-DD") from exc


def manual_date_range_from_args(args: argparse.Namespace) -> tuple[dt.date, dt.date]:
    if args.between is not None:
        if args.uncertainty:
            raise ValueError("--uncertainty kan bare brukes sammen med --date.")
        date_from, date_to = args.between
    else:
        date_value = args.date
        if args.uncertainty:
            date_from, date_to = date_range_from_uncertainty(date_value, args.uncertainty)
        else:
            date_from = date_value
            date_to = date_value
    if date_from > date_to:
        raise ValueError("Fra-dato kan ikke være etter til-dato.")
    return date_from, date_to


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


def manual_date_range_text(date_from: dt.date, date_to: dt.date) -> str:
    if date_from == date_to:
        return date_from.isoformat()
    midpoint = date_from + (date_to - date_from) // 2
    uncertainty_days = max((date_to - date_from).days // 2, 1)
    return f"ca. {midpoint.isoformat()} ± {uncertainty_days} dager ({date_from.isoformat()} til {date_to.isoformat()})"


def manual_date_from_row(row) -> str:
    try:
        date_from = dt.date.fromisoformat(str(row["manual_date_from"] or ""))
        date_to = dt.date.fromisoformat(str(row["manual_date_to"] or ""))
    except (ValueError, KeyError, IndexError):
        return ""
    return manual_date_range_text(date_from, date_to)


def h3_resolution_arg(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("må være et heltall") from exc
    if not 0 <= number <= 11:
        raise argparse.ArgumentTypeError("må være mellom 0 og 11")
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
    print(summary_line(stats))


def summary_line(stats) -> str:
    return (
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
    print("Bildesamling")
    print(f"  Totalt: {counts['total']}")
    print(f"  Bilder: {media['bilder']}")
    print(f"  Videoer: {media['videoer']}")
    print("\nKilder")
    print(f"  Kilder: {db.count_rows(conn, 'sources')}")
    print(f"  Importerte filer: {db.count_rows(conn, 'files')}")
    print(f"  Kildefilforekomster: {db.count_rows(conn, 'file_sources')}")
    print(f"  Duplikatkilder: {db.duplicate_source_count(conn)}")
    print("\nKontroll")
    print(f"  Uløste feil: {db.error_count(conn)}")
    name_conflicts = conn.execute("SELECT COUNT(*) FROM files WHERE name_conflict = 1").fetchone()[0]
    undated = conn.execute("SELECT COUNT(*) FROM files WHERE date_source = 'unknown'").fetchone()[0]
    print(f"  Navnekollisjoner: {name_conflicts}")
    print(f"  Filer uten dato: {undated}")
    print("\nDatokilde:")
    for source in ("metadata", "filename", "mtime", "unknown"):
        print(f"  {source}: {date_sources.get(source, 0)}")
    extra_sources = sorted(set(date_sources) - {"metadata", "filename", "mtime", "unknown"})
    for source in extra_sources:
        print(f"  {source}: {date_sources[source]}")


def print_refresh_summary(stats, *, dry_run: bool) -> None:
    prefix = "Dry-run: " if dry_run else ""
    print(
        prefix
        + "Oppsummering: "
        f"sjekket={stats.checked}, metadata_funnet={stats.metadata_found}, "
        f"flyttet={stats.moved}, allerede_riktig={stats.already_correct}, feil={stats.errors}"
    )


def print_refresh_metadata_progress(
    stage: str,
    current: int,
    total: int,
    stats,
    path: Path | None,
) -> None:
    global REFRESH_METADATA_PROGRESS
    if stage == "start":
        REFRESH_METADATA_PROGRESS = ProgressMeter("Refresh-metadata")
        REFRESH_METADATA_PROGRESS.message(f"Refresh-metadata: {total} filer skal kontrolleres.")
        return
    if REFRESH_METADATA_PROGRESS is None:
        REFRESH_METADATA_PROGRESS = ProgressMeter("Refresh-metadata")
    if stage == "error":
        REFRESH_METADATA_PROGRESS.error(f"Refresh-metadata-feil: {path}")
        return
    if stage == "check":
        REFRESH_METADATA_PROGRESS.update(
            current,
            total,
            action="kontrollert",
            details=(
                f"metadata_funnet={stats.metadata_found}, flyttet={stats.moved}, "
                f"allerede_riktig={stats.already_correct}, feil={stats.errors}"
            ),
            eta=True,
        )
        return
    if stage == "done":
        REFRESH_METADATA_PROGRESS.done(
            f"Refresh-metadata: ferdig kontrollert {min(current, total)}/{total} filer."
        )
        REFRESH_METADATA_PROGRESS = None
        return
