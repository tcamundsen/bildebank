from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import os
import re
import sqlite3
import sys
import traceback
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath

from . import __version__, db
from .backup import run_backup
from .cli_check_source import run_check_source
from .cli_doctor import run_doctor
from .cli_face import run_download_face_model, run_face_command
from .cli_geo import h3_resolution_arg, run_geo_command
from .cli_image import run_image_command
from .cli_server import run_server_command
from .cli_update import run_update
from .config import load_config, set_config_enabled
from .exiftool import install_managed_exiftool
from .exiftool_probe import exiftool_metadata_gaps
from .export_person import PersonExportInterrupted
from .face import (
    LEGACY_FACE_DB_FILENAME,
    face_database_dir,
)
from .file_lifecycle import remove_file, undelete_file
from .file_moves import recover_pending_file_moves
from .file_tags import set_file_tag
from .formatting import format_bytes
from .geo import DEFAULT_EXIFTOOL_BATCH_SIZE
from .importer import (
    import_source,
    import_source_dry_run,
    refresh_non_metadata_files,
    rescan_source,
    rescan_source_dry_run,
    validate_source_target,
)
from .html_export import export_html, export_html_conflicts
from .media import explain_date, inspect_metadata
from .media_cache import MediaMetadataCache, cached_image_dimensions
from .manual_dates import date_range_from_uncertainty
from .openclip import openclip_db_path
from .platform_guard import validate_collection_platform
from .pending_deletes import cleanup_pending_deletes, list_pending_deletes
from .progress import ProgressMeter
from .program_state import known_targets, program_db_path, record_target_best_effort
from .server import DEFAULT_HOST, DEFAULT_PORT
from .target_lock import TargetLock
from .thumbnails import ThumbnailStats, run_make_thumbnails
from .unimport import run_unimport as execute_unimport


THUMBNAIL_PROGRESS: ProgressMeter | None = None
REFRESH_METADATA_PROGRESS: ProgressMeter | None = None
UNIMPORT_SOURCE_PROGRESS: ProgressMeter | None = None
UNIMPORT_TARGET_PROGRESS: ProgressMeter | None = None


HELP_COMMAND_GROUPS = (
    (
        "kom i gang",
        (
            ("launcher", "Åpne Windows-vennlig kontrollpanel"),
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
            ("rescan-source", "Scan en tidligere importert kilde på nytt"),
        ),
    ),
    (
        "rydde trygt",
        (
            ("remove", "Flytt en importert fil til deleted/"),
            ("undelete", "Flytt en slettet fil tilbake fra deleted/"),
            ("unimport", "Reverser en tidligere importert kilde"),
            ("list-removed", "List filer som er slettet med `remove`"),
            ("cleanup-pending-deletes", "Kontroller eller kjør ventende filsletting"),
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
            ("download-face-model", "Last ned valgt InsightFace-modell"),
            ("face-scan", "Scanning etter ansikter"),
            ("face-suggest", "Foreslå personer for ukjente ansikter"),
            ("face-report", "Vis rapport for scannede ansikter"),
            ("face-person-list", "List personer i ansiktsdatabasen"),
            ("face-person-create", "Opprett person i ansiktsdatabasen"),
            ("face-person-add-face", "Koble ett ansikt til person"),
            ("face-person-remove-face", "Fjern ett ansikt fra person"),
            ("face-person-delete", "Slett person fra ansiktsdatabasen"),
            ("face-person-rename", "Endre navn på person i ansiktsdatabasen"),
            ("export-person", "Eksporter bilder av en person"),
            ("face-reset", "Slett ansiktsdata"),
        ),
    ),
    (
        "bildesøk",
        (
            ("image-scan", "Scan bilder for tekstbasert bildesøk"),
            ("image-search", "Søk etter bilder med tekst"),
            ("cleanup-image-search", "Rydd foreldreløse bildesøk-rader"),
        ),
    ),
    (
        "HTML-eksport",
        (
            ("make-thumbnails", "Lag thumbnails for rask månedsvisning"),
            ("make-browser", "Lag index.html for nettleseren"),
            ("make-conflict-browser", "Lag HTML-side for navnekollisjoner"),
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
            ("vacuum", "Pakk databasene så SQLite-filene krymper fysisk"),
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
    validate_parsed_args(parser, args)

    try:
        return run(args)
    except PersonExportInterrupted as exc:
        print(f"Avbrutt. {exc}", file=sys.stderr)
        return 130
    except KeyboardInterrupt:
        print("Avbrutt.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI should present readable errors
        if args.debug:
            traceback.print_exception(type(exc), exc, exc.__traceback__)
        else:
            print(f"Feil: {exc}", file=sys.stderr)
        return 1


def validate_parsed_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if getattr(args, "command", None) == "run-server" and args.lan_share and args.host is not None:
        parser.error("--lan-share kan ikke brukes sammen med --host. Bruk --port hvis du vil velge port.")


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

    add_command(
        subparsers,
        "launcher",
        usage="bildebank launcher",
        description="Åpne Windows-vennlig kontrollpanel for Bildebank.",
        help="Åpne Windows-vennlig kontrollpanel",
    )

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
    check_source.add_argument(
        "--accept-deleted",
        action="store_true",
        help="Godta filer som finnes i bildesamlingens deleted/-mappe",
    )
    check_source.add_argument("path", metavar="mappe", type=Path, help="Kildemappen som skal kontrolleres")
    rescan_source_parser = add_command(
        subparsers,
        "rescan-source",
        usage="bildebank rescan-source [valg] --name navn",
        help="Scan en tidligere importert kilde på nytt",
        description=(
            "Scanner en tidligere importert kilde på nytt med dagens støttede filtyper. "
            "Kommandoen oppretter ikke en ny kilde og sletter ingenting."
        ),
    )
    rescan_source_parser.add_argument("--name", required=True, help="Navn på importen som skal scannes på nytt")
    rescan_source_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Vis importoppsummering uten å kopiere filer eller endre databasen",
    )
    rescan_source_parser.add_argument("--quiet", action="store_true", help="Ikke vis fremdrift under scanning")
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
    cleanup_pending = add_command(
        subparsers,
        "cleanup-pending-deletes",
        usage="bildebank cleanup-pending-deletes [valg]",
        help="Kontroller eller kjør ventende filsletting",
        description=(
            "Vis pending-delete-køen, eller prøv eksplisitt å slette filer "
            "som ikke lenger har database-referanser."
        ),
    )
    cleanup_mode = cleanup_pending.add_mutually_exclusive_group()
    cleanup_mode.add_argument(
        "--list",
        action="store_true",
        help="Vis pending filer og siste feilmelding. Dette er standard.",
    )
    cleanup_mode.add_argument(
        "--apply",
        action="store_true",
        help="Prøv å slette pending filer.",
    )
    cleanup_pending.add_argument(
        "--limit",
        type=positive_int_arg,
        help="Maks antall pending filer som forsøkes med --apply.",
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
        help="Scan bare filer uten GPS-data og uten tidligere GPS-resultat. Dette er standard.",
    )
    geo_scan.add_argument(
        "--retry-missing",
        action="store_true",
        help="Prøv også filer som tidligere ble scannet uten GPS eller med feil",
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
    browser.add_argument(
        "--hide-out-of-focus",
        action="store_true",
        help='Ikke ta med bilder tagget "Ute av fokus" i den statiske HTML-browseren.',
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
    doctor = add_command(
        subparsers,
        "doctor",
        usage="bildebank doctor [valg]",
        help="Vis diagnose for installasjon og aktiv bildesamling",
    )
    doctor.add_argument(
        "--deep",
        action="store_true",
        help="Kjør tregere filintegritetssjekker.",
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
    add_command(
        subparsers,
        "download-face-model",
        usage="bildebank download-face-model",
        help="Last ned valgt InsightFace-modell",
        description="Last ned ansiktsmodellen som er valgt i bildebank-config.toml.",
    )
    image_scan = add_command(
        subparsers,
        "image-scan",
        usage="bildebank image-scan [valg]",
        help="Scan bilder for tekstbasert bildesøk",
        description="Scan bilder for tekstbasert bildesøk",
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
    cleanup_image_search_parser = add_command(
        subparsers,
        "cleanup-image-search",
        usage="bildebank cleanup-image-search [valg]",
        help="Rydd foreldreløse bildesøk-rader",
        description=(
            "Vis eller slett OpenCLIP-rader som peker på filer som mangler "
            "i hoveddatabasen eller er markert som slettet."
        ),
    )
    cleanup_image_search_parser.add_argument(
        "--apply",
        action="store_true",
        help="Slett foreldreløse image_embeddings og image_search_results.",
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
    run_server_parser.add_argument(
        "--preview-images",
        action="store_true",
        help="Bruk nedskalerte preview-bilder i hovedvisningen.",
    )
    run_server_parser.add_argument(
        "--read-only",
        action="store_true",
        help="Vis bilder og metadata, men blokker innstillinger, administrasjon og endringer.",
    )
    run_server_parser.add_argument(
        "--lan-share",
        action="store_true",
        help="Del read-only på privat LAN med preview-bilder. Avviser --host, men kan brukes med --port.",
    )
    run_server_parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Tillat bevisst binding til en adresse som kan nås fra andre maskiner.",
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
    export_person_parser = add_command(
        subparsers,
        "export-person",
        usage="bildebank export-person [valg] navn --dest mappe",
        help="Eksporter bildene som vises for en person",
        description="Eksporter bildene som vises på personens side i bildebrowseren.",
    )
    export_person_parser.add_argument("name", metavar="navn", help="Personnavn")
    export_person_parser.add_argument(
        "--dest",
        required=True,
        type=Path,
        metavar="mappe",
        help="Eksisterende mappe som personmappen skal opprettes i",
    )
    export_person_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Vis planlagte kopier uten å opprette eller endre noe",
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
    person_browser.add_argument(
        "--hide-out-of-focus",
        action="store_true",
        help='Ikke ta med bilder tagget "Ute av fokus" i den statiske personbrowseren.',
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
    people_browser.add_argument(
        "--hide-out-of-focus",
        action="store_true",
        help='Ikke ta med bilder tagget "Ute av fokus" i de statiske personbrowserne.',
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
        help="Pakk databasene så SQLite-filene krymper fysisk",
        description="Kjører SQLite VACUUM på Bildebank-databasene. Kommandoen endrer ikke bildefiler.",
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
    if formatter_class is not None:
        return subparsers.add_parser(
            name,
            help=help,
            description=description,
            usage=usage,
            formatter_class=formatter_class,
        )
    return subparsers.add_parser(name, help=help, description=description, usage=usage)


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


NO_TARGET_COMMANDS = {
    "launcher",
    "create",
    "explain-date",
    "inspect-metadata",
    "update",
    "exiftool-install",
    "where-is",
    "doctor",
    "config",
    "download-face-model",
}

TARGET_COMMANDS = {
    "migrate",
    "backup",
    "vacuum",
    "geo-scan",
    "geo-stats",
    "geo-areas",
    "geo-area",
    "run-server",
    "check-source",
    "cleanup-pending-deletes",
}

IMAGE_COMMANDS = {"image-scan", "image-search", "cleanup-image-search"}

FACE_COMMANDS = {
    "face-scan",
    "face-report",
    "face-person-create",
    "face-person-add-face",
    "face-person-remove-face",
    "face-person-delete",
    "face-person-rename",
    "face-person-list",
    "export-person",
    "face-suggest",
    "make-person-browser",
    "make-people-browser",
    "face-reset",
}


def run(args: argparse.Namespace) -> int:
    if args.command in NO_TARGET_COMMANDS:
        return run_no_target_command(args)

    target = resolve_target(args.target)
    validate_collection_platform(target)
    if should_recover_pending_file_moves(args):
        recover_pending_file_moves(target)
    record_target_best_effort(program_repo_root(), target)

    if args.command in TARGET_COMMANDS or (
        args.command in {"import", "rescan-source"} and args.dry_run
    ):
        return run_target_command(args, target)

    if args.command in IMAGE_COMMANDS:
        return run_image_command(args, target, repo_root=program_repo_root())

    if args.command in FACE_COMMANDS:
        return run_face_command(args, target, repo_root=program_repo_root())

    if args.command in {"remove", "undelete"}:
        return run_file_lifecycle_command(args, target)

    if args.command == "unimport":
        return run_unimport_command(target, args)

    if args.command in {"tag-add", "tag-remove"}:
        return run_tag_mutation_command(args, target)

    return run_db_command(args, target)


def run_no_target_command(args: argparse.Namespace) -> int:
    if args.command == "launcher":
        from .launcher import main as launcher_main

        return launcher_main()

    if args.command == "create":
        target = args.path.resolve()
        validate_collection_platform(target)
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
        return run_update(program_repo_root())

    if args.command == "exiftool-install":
        return run_exiftool_install(force=args.force)

    if args.command == "where-is":
        return run_where_is()

    if args.command == "doctor":
        target = db.find_target(args.target)
        if target is not None:
            validate_collection_platform(target)
            recover_pending_file_moves(target)
        return run_doctor(target, deep=getattr(args, "deep", False), repo_root=program_repo_root())

    if args.command == "config":
        return run_config(args.section, enabled=args.action == "enable")

    if args.command == "download-face-model":
        return run_download_face_model(program_repo_root())

    raise ValueError(f"Ukjent kommando uten bildesamling: {args.command}")


def should_recover_pending_file_moves(args: argparse.Namespace) -> bool:
    if args.command == "migrate":
        return False
    if getattr(args, "dry_run", False):
        return False
    return True


def run_target_command(args: argparse.Namespace, target: Path) -> int:
    if args.command == "migrate":
        return run_migrate(target, check=args.check)

    if args.command == "backup":
        return run_backup_command(target, args.destination, dry_run=args.dry_run)

    if args.command == "vacuum":
        return run_vacuum(target)

    if args.command in {"geo-scan", "geo-stats", "geo-areas", "geo-area"}:
        return run_geo_command(args, target, repo_root=program_repo_root())

    if args.command == "run-server":
        lan_share = args.lan_share
        return run_server_command(
            target,
            host="0.0.0.0" if lan_share else (args.host or DEFAULT_HOST),
            port=args.port,
            repo_root=program_repo_root(),
            browser=not args.no_browser,
            allow_remote=args.allow_remote or lan_share,
            preview_images=args.preview_images or lan_share,
            read_only=args.read_only or lan_share,
            lan_share=lan_share,
        )

    if args.command == "cleanup-pending-deletes":
        return run_cleanup_pending_deletes(
            target,
            apply=args.apply,
            limit=args.limit,
        )

    if args.command == "import":
        return run_named_import_dry_run(target, args)

    if args.command == "rescan-source":
        return run_rescan_source_dry_run(target, args)

    return run_check_source(
        target,
        args.path,
        verbose=not args.quiet,
        accept_deleted=args.accept_deleted,
        path_adapter=existing_path_arg,
    )


def run_file_lifecycle_command(args: argparse.Namespace, target: Path) -> int:
    if args.command == "remove":
        result_path = remove_file(
            target,
            collection_path=args.path,
            command_args=vars_for_log(args),
            path_adapter=existing_path_arg,
        )
        print(f"Flyttet til slettet mappe: {target / result_path}")
        return 0

    result_path = undelete_file(
        target,
        collection_path=args.path,
        command_args=vars_for_log(args),
        path_adapter=existing_path_arg,
    )
    print(f"Flyttet tilbake til bildesamlingen: {target / result_path}")
    return 0


def run_unimport_command(target: Path, args: argparse.Namespace) -> int:
    def confirm(plan: db.UnimportPlan) -> bool:
        print_unimport_plan(target, plan)
        return input('Skriv "ja, det vil jeg" for å gjennomføre unimport: ') == "ja, det vil jeg"

    result = execute_unimport(
        target,
        args.name,
        config=load_config(program_repo_root()),
        dry_run=args.dry_run,
        confirm=confirm,
        source_progress=unimport_source_progress(),
        target_progress=unimport_target_progress(),
    )
    if args.dry_run:
        print_unimport_plan(target, result.plan)
        print_unimport_dry_run_note(result.plan.source)
        print("Dry-run: ingen endringer er gjort.")
        return 0
    if not result.applied:
        print("Avbrutt. Ingen endringer er gjort.")
        return 0

    failed = [item for item in result.cleanup_results if item.outcome == "failed"]
    print("Unimport gjennomført.")
    print("Kilden er fjernet fra kildelisten.")
    if failed:
        print(
            f"{len(failed)} fil(er) kunne ikke slettes og ligger fortsatt i "
            "pending-delete-køen."
        )
        for item in failed:
            print(f"  {item.path.as_posix()}: {item.error}")
    return 0


def run_tag_mutation_command(args: argparse.Namespace, target: Path) -> int:
    tagged = args.command == "tag-add"
    result = set_file_tag(
        target,
        collection_path=args.path,
        tag_name=args.tag,
        tagged=tagged,
        command_args=vars_for_log(args),
        path_adapter=existing_path_arg,
    )
    if tagged:
        action = "La til" if result.changed else "Fantes allerede"
    else:
        action = "Fjernet" if result.changed else "Fant ikke"
    print(f"{action}: {result.tag_name} -> {db.absolute_target_path(target, result.target_path)}")
    return 0


def run_db_command(args: argparse.Namespace, target: Path) -> int:
    if args.command == "import":
        return run_import_command(args, target)
    if args.command == "rescan-source":
        return run_rescan_source_command(args, target)
    if args.command in {"date-set", "date-clear"}:
        return run_manual_date_command(args, target)
    if args.command == "refresh-metadata":
        return run_refresh_metadata_command(args, target)
    if args.command == "make-browser":
        return run_make_browser_command(args, target)
    if args.command == "make-thumbnails":
        return run_make_thumbnails_command(args, target)
    if args.command == "make-conflict-browser":
        return run_make_conflict_browser_command(args, target)

    conn = db.connect(target)
    try:
        if not (args.command == "unimport" and args.dry_run):
            db.log_command(conn, args.command, vars_for_log(args))

        if args.command == "list-sources":
            for listed_source in db.get_sources(conn):
                imported = listed_source.imported_at or "-"
                superseded_by = listed_source.superseded_by_source_id or "-"
                print(
                    f"{listed_source.id}\t{listed_source.status}\t"
                    f"{imported}\t{superseded_by}\t"
                    f"{listed_source.name}\t{listed_source.path}"
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
            conflict_row = db.file_by_target_path(conn, target, path)
            if conflict_row is None:
                raise ValueError(f"Filen finnes ikke i importdatabasen: {path}")
            rows = name_conflict_group(conn, conflict_row)
            if len(rows) < 2 or not any(item["name_conflict"] for item in rows):
                print(f"Filen er ikke del av en navnekollisjon: {path}")
                return 0
            print(f"Navnekollisjon: {conflict_row['original_filename']}")
            print(
                "Mappe i bildesamlingen: "
                f"{db.absolute_target_path(target, Path(str(conflict_row['target_path']))).parent}"
            )
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

        if args.command == "list-removed":
            for row in db.deleted_files(conn):
                print_deleted_item(target, row)
            return 0

        if args.command == "non-metadata":
            for row in db.non_metadata_files(conn):
                taken_date = row["taken_date"] or "-"
                print(f"{db.absolute_target_path(target, Path(str(row['target_path'])))}")
                print(f"  dato: {row['date_source']}\t{taken_date}")
                if args.with_source:
                    print(f"  kilde: {row['source_path']}")
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

        if args.command == "errors":
            for row in db.errors(
                conn,
                limit=args.limit,
                stage=args.stage,
                include_resolved=args.include_resolved,
            ):
                error_source_path = row["source_path"] or "-"
                resolved = row["resolved_at"] or "-"
                print(
                    f"{row['id']}\t{row['created_at']}\t{row['stage']}\t"
                    f"{resolved}\t{error_source_path}\t{row['message']}"
                )
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


def run_import_command(args: argparse.Namespace, target: Path) -> int:
    with TargetLock(target, command="import"):
        conn = db.connect(target)
        try:
            db.log_command(conn, args.command, vars_for_log(args))
            source_path = existing_path_arg(args.path).resolve()
            if not source_path.is_dir():
                raise ValueError(f"Kilden finnes ikke som mappe: {source_path}")
            validate_source_target(source_path, target)
            source_id = db.add_named_source(conn, source_path, args.name)
            conn.commit()
            print(f"Registrert kilde #{source_id}: {args.name} ({source_path})")
            source_row = db.get_source(conn, source_id)
            if source_row.imported_at is not None:
                print(f"Kilden er allerede importert: {args.name}")
                return 0
            import_stats = import_source(conn, target, source_row, verbose=not args.quiet)
        finally:
            conn.close()
    print_summary(import_stats)
    return 0 if import_stats.errors == 0 else 2


def run_rescan_source_command(args: argparse.Namespace, target: Path) -> int:
    with TargetLock(target, command="rescan-source"):
        conn = db.connect(target)
        try:
            db.log_command(conn, args.command, vars_for_log(args))
            selected_source = resolve_rescan_source(conn, args.name)
            validate_source_target(selected_source.path, target)
            print(
                f"Scanner kilde på nytt #{selected_source.id}: "
                f"{selected_source.name} ({selected_source.path})"
            )
            rescan_stats = rescan_source(
                conn,
                target,
                selected_source,
                verbose=not args.quiet,
            )
        finally:
            conn.close()
    print_summary(rescan_stats)
    return 0 if rescan_stats.errors == 0 else 2


def run_manual_date_command(args: argparse.Namespace, target: Path) -> int:
    command = str(args.command)
    with TargetLock(target, command=command):
        conn = db.connect(target)
        try:
            db.log_command(conn, command, vars_for_log(args))
            if command == "date-set":
                date_from, date_to = manual_date_range_from_args(args)
                row = resolve_db_file_for_tag_command(conn, target, args.path)
                db.set_manual_date(
                    conn,
                    file_id=int(row["id"]),
                    date_from=date_from.isoformat(),
                    date_to=date_to.isoformat(),
                    note=args.note,
                )
                conn.commit()
            else:
                row = resolve_db_file_for_tag_command(conn, target, args.path)
                db.clear_manual_date(conn, file_id=int(row["id"]))
                conn.commit()
        finally:
            conn.close()
    if command == "date-set":
        print(f"Manuell dato satt: {manual_date_range_text(date_from, date_to)}")
        print(f"Fil: {db.absolute_target_path(target, Path(str(row['target_path'])))}")
        if args.note:
            print(f"Notat: {db.clean_manual_date_note(args.note)}")
    else:
        print(f"Manuell dato fjernet: {db.absolute_target_path(target, Path(str(row['target_path'])))}")
    return 0


def run_refresh_metadata_command(args: argparse.Namespace, target: Path) -> int:
    if args.dry_run:
        refresh_stats = refresh_non_metadata_files(
            target,
            dry_run=True,
            rescan=args.rescan,
            verbose=args.verbose,
            progress=print_refresh_metadata_progress,
        )
    else:
        with TargetLock(target, command="refresh-metadata"):
            conn = db.connect(target)
            try:
                db.log_command(conn, args.command, vars_for_log(args))
                conn.commit()
            finally:
                conn.close()
            refresh_stats = refresh_non_metadata_files(
                target,
                dry_run=False,
                rescan=args.rescan,
                verbose=args.verbose,
                progress=print_refresh_metadata_progress,
                target_locked=True,
            )
    print_refresh_summary(refresh_stats, dry_run=args.dry_run)
    if refresh_stats.stopped:
        return 130
    return 0 if refresh_stats.errors == 0 else 2


def run_make_browser_command(args: argparse.Namespace, target: Path) -> int:
    output = args.output.resolve() if args.output else None
    with TargetLock(target, command="make-browser"):
        conn = db.connect(target)
        try:
            db.log_command(conn, args.command, vars_for_log(args))
            conn.commit()
        finally:
            conn.close()
        output_path = export_html(
            target,
            output,
            month_preview_limit=args.month_preview_limit,
            hide_out_of_focus=args.hide_out_of_focus,
            debug_timing=args.debug,
        )
    print(f"Skrev HTML-browser: {output_path}")
    return 0


def run_make_thumbnails_command(args: argparse.Namespace, target: Path) -> int:
    with TargetLock(target, command="make-thumbnails"):
        conn = db.connect(target)
        try:
            db.log_command(conn, args.command, vars_for_log(args))
            conn.commit()
        finally:
            conn.close()
        thumbnail_stats = run_make_thumbnails(
            target,
            limit=args.limit,
            verbose=args.verbose,
            progress=print_thumbnail_progress,
            target_locked=True,
        )
    print_thumbnail_summary(thumbnail_stats)
    return 0 if thumbnail_stats.errors == 0 else 2


def run_make_conflict_browser_command(args: argparse.Namespace, target: Path) -> int:
    output = args.output.resolve() if args.output else None
    with TargetLock(target, command="make-conflict-browser"):
        conn = db.connect(target)
        try:
            db.log_command(conn, args.command, vars_for_log(args))
            conn.commit()
        finally:
            conn.close()
        output_path = export_html_conflicts(target, output, target_locked=True)
    print(f"Skrev HTML-browser for navnekollisjoner: {output_path}")
    return 0


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
    found_target = db.find_target()
    if found_target is None:
        raise ValueError("Fant ingen bildesamling. Kjør kommandoen fra bildesamlingsmappen.")
    return found_target


def resolve_source_by_name(conn, name: str) -> db.Source:
    source = db.find_source_by_name(conn, name)
    if source is None:
        raise ValueError(f"Fant ikke kilde med navn: {name}")
    return source


def resolve_rescan_source(conn, name: str) -> db.Source:
    source = resolve_source_by_name(conn, name)
    if source.superseded_by_source_id is not None or source.status == "superseded":
        raise ValueError(
            f"Kilden {name!r} er markert som erstattet av kilde #{source.superseded_by_source_id}. "
            "Scan den aktive overkilden i stedet."
        )
    source_path = existing_path_arg(source.path).resolve()
    if not source_path.exists() or not source_path.is_dir():
        raise ValueError(f"Kilden finnes ikke som mappe: {source.path}")
    return replace(source, path=source_path)


def run_rescan_source_dry_run(target: Path, args: argparse.Namespace) -> int:
    conn = db.connect(target)
    try:
        source = resolve_rescan_source(conn, args.name)
        validate_source_target(source.path, target)
        stats = rescan_source_dry_run(
            conn, target, source, output=sys.stdout, verbose=not args.quiet
        )
        print_summary(stats)
        return 0 if stats.errors == 0 else 2
    finally:
        conn.close()


def unimport_source_progress() -> ProgressMeter:
    global UNIMPORT_SOURCE_PROGRESS
    UNIMPORT_SOURCE_PROGRESS = ProgressMeter("Unimport")
    return UNIMPORT_SOURCE_PROGRESS


def unimport_target_progress() -> ProgressMeter:
    global UNIMPORT_TARGET_PROGRESS
    UNIMPORT_TARGET_PROGRESS = ProgressMeter("Unimport")
    return UNIMPORT_TARGET_PROGRESS


def print_unimport_plan(target: Path, plan: db.UnimportPlan) -> None:
    print(f"Kilde: {plan.source.name or plan.source.path}")
    print(f"Registrerte kildefiler kontrollert: {plan.source_file_count}")
    print(f"Items/importkoblinger som fjernes: {plan.source_file_count}")
    print(f"Filer som fjernes fra aktiv samling: {plan.active_remove_count}")
    print(
        "Filer som blir liggende fordi de også finnes i andre kilder: "
        f"{plan.active_keep_count}"
    )
    print("Filer som legges i pending_file_deletes:")
    if plan.target_paths_to_delete:
        for path in plan.target_paths_to_delete:
            print(f"  {db.target_relative_path(target, path).as_posix()}")
    else:
        print("  ingen")
    print("Filer som beholdes fordi de fortsatt har referanser:")
    if plan.target_paths_to_keep:
        for path in plan.target_paths_to_keep:
            print(f"  {db.target_relative_path(target, path).as_posix()}")
    else:
        print("  ingen")


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


@dataclass(frozen=True)
class VacuumDatabase:
    label: str
    path: Path


def vacuum_databases(target: Path) -> list[VacuumDatabase]:
    config = load_config(program_repo_root())
    databases = [VacuumDatabase("Hoveddatabase", db.db_path_for_target(target))]

    openclip_path = openclip_db_path(target)
    if openclip_path.is_file():
        databases.append(VacuumDatabase("Bildesøkdatabase", openclip_path))

    legacy_face_path = target / LEGACY_FACE_DB_FILENAME
    if legacy_face_path.is_file():
        databases.append(VacuumDatabase("Ansiktsdatabase", legacy_face_path))

    face_dir = face_database_dir(target, config.face_recognition)
    if face_dir.is_dir():
        for face_path in sorted(face_dir.glob("*.sqlite3")):
            if face_path.is_file():
                databases.append(VacuumDatabase("Ansiktsdatabase", face_path))

    seen: set[Path] = set()
    unique_databases: list[VacuumDatabase] = []
    for database in databases:
        key = database.path.resolve()
        if key in seen:
            continue
        seen.add(key)
        unique_databases.append(database)
    return unique_databases


def vacuum_sqlite_database(database_path: Path) -> tuple[int, int]:
    before = database_path.stat().st_size
    conn = sqlite3.connect(database_path)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
    after = database_path.stat().st_size
    return before, after


def run_vacuum(target: Path) -> int:
    with TargetLock(target, command="vacuum"):
        for database in vacuum_databases(target):
            before, after = vacuum_sqlite_database(database.path)
            print(f"Database: {database.label}")
            print(f"Sti: {database.path}")
            print(f"Størrelse før:  {format_bytes(before)}")
            print(f"Størrelse etter: {format_bytes(after)}")
            print()
    print("Ferdig. Databasene er pakket.")
    return 0


def run_config(section: str, *, enabled: bool) -> int:
    repo_root = program_repo_root()
    config_path = set_config_enabled(repo_root, section, enabled)
    print(f"{section}.enabled er satt til {_toml_bool_text(enabled)}.")
    print(f"Config-fil: {config_path}")
    return 0


def _toml_bool_text(value: bool) -> str:
    return "true" if value else "false"


def python_module_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def module_available(module_name: str) -> str:
    return "ja" if python_module_available(module_name) else "nei"


def run_cleanup_pending_deletes(
    target: Path,
    *,
    apply: bool,
    limit: int | None,
) -> int:
    if not apply:
        rows = list_pending_deletes(target)
        if not rows:
            print("Ingen pending filslettinger.")
            return 0
        for row in rows:
            print(f"#{row.id}\t{row.path.as_posix()}")
            print(f"  årsak: {row.reason}")
            print(f"  forsøk: {row.attempts}")
            if row.source_id is not None:
                print(f"  kilde-id: {row.source_id}")
            if row.last_error:
                print(f"  siste feil: {row.last_error}")
        return 0

    results = cleanup_pending_deletes(target, limit=limit)
    deleted = sum(result.outcome == "deleted" for result in results)
    missing = sum(result.outcome == "missing" for result in results)
    failed = sum(result.outcome == "failed" for result in results)
    for result in results:
        if result.outcome == "failed":
            print(f"FEIL\t{result.path.as_posix()}\t{result.error}")
        else:
            print(f"{result.outcome.upper()}\t{result.path.as_posix()}")
    print(
        "Pending filsletting: "
        f"kontrollert={len(results)}, slettet={deleted}, manglet={missing}, feil={failed}"
    )
    return 0 if failed == 0 else 2


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


def run_migrate(target: Path, *, check: bool) -> int:
    plan = db.migration_plan(target, validate=check)
    print(f"Bildesamling: {target}")
    print(f"Database: {db.db_path_for_target(target)}")
    print(f"Nåværende schema_version: {plan.current_version}")
    print(f"Ny schema_version: {plan.target_version}")
    if (
        plan.current_version == plan.target_version
        and not plan.refreshes_performance_indexes
        and not plan.internal_repairs
    ):
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
    if plan.creates_pending_file_deletes:
        print("  opprette pending_file_deletes")
    if plan.creates_pending_file_moves:
        print("  opprette pending_file_moves")
    if plan.adds_metadata_datetime_column:
        print("  legge til metadata_datetime i files")
    if plan.refreshes_performance_indexes:
        print("  oppdatere manglende ytelsesindekser")
    for repair in plan.internal_repairs:
        print(f"  reparere intern v{plan.target_version}-struktur: {repair}")
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
    if result.creates_pending_file_deletes:
        print("Oppretter pending_file_deletes.")
    if result.creates_pending_file_moves:
        print("Oppretter pending_file_moves.")
    if result.adds_metadata_datetime_column:
        print("Legger til metadata_datetime i files.")
        print("Kjør bildebank refresh-metadata --rescan for å fylle tidspunkt for eksisterende filer.")
    if result.refreshes_performance_indexes:
        print("Oppdaterer manglende ytelsesindekser.")
    if result.internal_repairs:
        print(f"Reparerer intern v{result.target_version}-struktur.")
    print(f"Setter schema_version={result.target_version}.")
    print("Ferdig. Databasen er migrert.")
    if result.cleans_gps_errors:
        print("Kjør bildebank vacuum hvis du vil krympe SQLite-filen fysisk.")
    return 0


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


def existing_path_arg(path: Path) -> Path:
    if path.exists():
        return path
    raw = str(path)
    stripped = raw.rstrip("\"'")
    if stripped != raw:
        candidate = Path(stripped)
        if candidate.exists():
            return candidate
    for value in (raw, stripped):
        wsl_candidate = wsl_path_from_windows_path(value)
        if wsl_candidate is not None and wsl_candidate.exists():
            return wsl_candidate
    return path


def wsl_path_from_windows_path(value: str) -> Path | None:
    if os.name == "nt":
        return None
    windows_path = PureWindowsPath(value)
    drive = windows_path.drive
    if not re.fullmatch(r"[A-Za-z]:", drive):
        return None
    parts = list(windows_path.parts)
    if parts and parts[0] == windows_path.anchor:
        parts = parts[1:]
    return Path("/mnt") / drive[0].lower() / Path(*parts)


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
    if not isinstance(media, dict):
        raise ValueError("Ugyldig statusdata: media må være en dict.")
    if not isinstance(date_sources, dict):
        raise ValueError("Ugyldig statusdata: date_sources må være en dict.")
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
    stopped = ", avbrutt=ja" if stats.stopped else ""
    print(
        prefix
        + "Oppsummering: "
        f"sjekket={stats.checked}, metadata_funnet={stats.metadata_found}, "
        f"flyttet={stats.moved}, allerede_riktig={stats.already_correct}, feil={stats.errors}"
        f"{stopped}"
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
