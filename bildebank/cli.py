from __future__ import annotations

import argparse
import datetime as dt
import json
import importlib.util
import os
import re
import signal
import sqlite3
import sys
import traceback
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath

from . import __version__, db
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
from .ffmpeg_tools import FFmpegTools, install_managed_ffmpeg, resolve_ffmpeg_tools
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
from .html_export import export_html
from .media import explain_date, inspect_metadata
from .media_cache import MediaMetadataCache, cached_image_dimensions
from .manual_dates import date_range_from_uncertainty
from .openclip import openclip_db_path
from .platform_guard import validate_collection_platform
from .pending_deletes import cleanup_pending_deletes, list_pending_deletes
from .progress import ProgressMeter
from .program_state import (
    known_targets,
    program_db_path,
    record_published_snapshot_best_effort,
    record_target_best_effort,
)
from .server_runtime import DEFAULT_HOST, DEFAULT_PORT
from .server_slideshow import DEFAULT_SLIDESHOW_DELAY_SECONDS
from .snapshot import MainDatabaseSourceError, SnapshotPlan, plan_snapshot
from .snapshot_check import (
    SnapshotCheckProgress,
    SnapshotCheckResult,
    check_snapshot_repository,
    list_repository_snapshots,
)
from .snapshot_create import (
    SnapshotCreationResult,
    create_snapshot,
    validate_existing_recovery_repository,
)
from .snapshot_progress import SnapshotCreateProgress, SnapshotPlanProgress
from .snapshot_restore import (
    FullRestorePlan,
    FullRestoreResult,
    SingleFileRestorePlan,
    SingleFileRestoreResult,
    SnapshotProblemsResult,
    list_snapshot_problems,
    plan_full_restore,
    plan_single_file_restore,
    restore_full_snapshot,
    restore_single_file,
)
from .target_lock import TargetLock
from .thumbnails import ThumbnailStats, run_make_thumbnails
from .unimport import TargetContentChange, run_unimport as execute_unimport
from .video_previews import VideoPreviewStats, run_make_video_previews


THUMBNAIL_PROGRESS: ProgressMeter | None = None
VIDEO_PREVIEW_PROGRESS: ProgressMeter | None = None
REFRESH_METADATA_PROGRESS: ProgressMeter | None = None
UNIMPORT_SOURCE_PROGRESS: ProgressMeter | None = None
UNIMPORT_TARGET_PROGRESS: ProgressMeter | None = None


class BildebankHelpFormatter(argparse.RawDescriptionHelpFormatter):
    def _format_action(self, action: argparse.Action) -> str:
        if isinstance(action, argparse._SubParsersAction):
            return ""
        return super()._format_action(action)


def install_windows_interrupt_handler() -> None:
    """Let launcher Ctrl+Break unwind command cleanup like Ctrl+C."""
    if os.name != "nt":
        return
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        signal.signal(sigbreak, signal.default_int_handler)


def main(argv: list[str] | None = None) -> int:
    install_windows_interrupt_handler()
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
    if getattr(args, "command", None) != "run-server":
        return
    if (args.lan_share or args.slideshow) and args.host is not None:
        option = "--slideshow" if args.slideshow else "--lan-share"
        parser.error(f"{option} kan ikke brukes sammen med --host. Bruk --port hvis du vil velge port.")
    if not args.slideshow and args.slideshow_filter is not None:
        parser.error("--filter kan bare brukes sammen med --slideshow.")
    if not args.slideshow and args.delay is not None:
        parser.error("--delay kan bare brukes sammen med --slideshow.")


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
        "start",
        usage="bildebank start",
        description="Åpne Bildebank-vinduet.",
        help="Åpne Bildebank-vinduet",
    )
    add_command(
        subparsers,
        "launcher",
        usage="bildebank launcher",
        description="Alias for bildebank start.",
        help=argparse.SUPPRESS,
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
        help="Vis alle filer i kilder med samme navnekollisjon som en importert fil",
    )
    show_conflict.add_argument("path", metavar="fil", type=Path, help="Importert fil")
    show_source = add_command(
        subparsers,
        "show-source",
        usage="bildebank show-source [valg] fil",
        help="Vis hvilken kilde en importert fil kommer fra",
        description="Vis hvilken kilde en importert fil kommer fra",
    )
    show_source.add_argument("path", metavar="fil", type=Path, help="Importert fil")
    date_set = add_command(
        subparsers,
        "date-set",
        usage="bildebank date-set [valg] fil (--date DATO | --between FRA TIL)",
        help="Sett manuell dato for en importert fil",
        description="Sett manuell dato i Bildebank uten å endre originalfilen.",
    )
    date_set.add_argument("path", metavar="fil", type=Path, help="Importert fil")
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
    date_clear.add_argument("path", metavar="fil", type=Path, help="Importert fil")
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
            "Kontrollerer først at alle registrerte originalfiler fortsatt finnes "
            "med samme innhold. Krever nøyaktig bekreftelse før noe endres."
        ),
    )
    unimport.add_argument("--name", required=True, help="Navn på importen som skal reverseres")
    unimport.add_argument(
        "--dry-run",
        action="store_true",
        help="Vis hva som ville blitt gjort uten å slette filer eller endre databasen",
    )
    unimport.add_argument(
        "--target-change-report-json",
        type=Path,
        help=argparse.SUPPRESS,
    )
    remove = add_command(
        subparsers,
        "remove",
        usage="bildebank remove [valg] fil",
        help="Flytt en importert fil til deleted/",
        description="Flytt en importert fil til deleted/ og marker den som slettet",
    )
    remove.add_argument("path", metavar="fil", type=Path, help="Importert fil som skal fjernes")
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
        help="Vis filen i kilden i tillegg til importert fil",
    )
    tag_list = add_command(
        subparsers,
        "tag-list",
        usage="bildebank tag-list [valg] [fil]",
        help="List tagger eller tagger for én fil",
    )
    tag_list.add_argument("path", metavar="fil", type=Path, nargs="?", help="Importert fil")
    tag_add = add_command(
        subparsers,
        "tag-add",
        usage="bildebank tag-add [valg] fil tagg",
        help="Legg tagg på en importert fil",
    )
    tag_add.add_argument("path", metavar="fil", type=Path, help="Importert fil")
    tag_add.add_argument("tag", metavar="tagg", help="Taggnavn")
    tag_remove = add_command(
        subparsers,
        "tag-remove",
        usage="bildebank tag-remove [valg] fil tagg",
        help="Fjern tagg fra en importert fil",
    )
    tag_remove.add_argument("path", metavar="fil", type=Path, help="Importert fil")
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

    make_video_previews = add_command(
        subparsers,
        "make-video-previews",
        usage="bildebank make-video-previews [valg]",
        help="Lag MP4-avspillingskopier av AVI- og 3GP-videoer",
        description="Lag regenererbare MP4-kopier av aktive AVI- og 3GP-videoer for nettleseren.",
    )
    make_video_previews.add_argument(
        "--dry-run",
        action="store_true",
        help="Vis hva som mangler uten å installere programmer eller skrive filer.",
    )
    make_video_previews.add_argument(
        "--limit",
        type=positive_int_arg,
        help="Maks antall AVI- og 3GP-filer som skal kontrolleres.",
    )
    make_video_previews.add_argument("--verbose", action="store_true", help="Vis filer som feiler.")
    make_video_previews.add_argument(
        "--rebuild",
        action="store_true",
        help="Lag alle AVI- og 3GP-avspillingskopier på nytt.",
    )

    add_command(
        subparsers,
        "where-is",
        usage="bildebank where-is [valg]",
        help="Vis hvor Bildebank og kjente bildesamlinger ligger",
        description="Vis hvor Bildebank og kjente bildesamlinger ligger",
    )
    snapshot = add_command(
        subparsers,
        "snapshot",
        usage="bildebank snapshot <kommando> [valg]",
        help="Lag og kontroller versjonerte snapshots",
        description="Opprett, kontroller og gjenopprett snapshots.",
    )
    snapshot_subparsers = snapshot.add_subparsers(dest="snapshot_command", required=True)
    snapshot_create = snapshot_subparsers.add_parser(
        "create",
        usage="bildebank snapshot create [valg] repository",
        description="Planlegg eller opprett et versjonert snapshot.",
    )
    snapshot_create.add_argument(
        "repository",
        metavar="repository",
        type=Path,
        help="Den eksakte repositorymappen",
    )
    snapshot_create.add_argument(
        "--dry-run",
        action="store_true",
        help="Valider og estimer uten å skrive filer eller ta låser",
    )
    snapshot_create.add_argument(
        "--note",
        help="Valgfri, uforanderlig kommentar på høyst 1000 tegn",
    )
    snapshot_create.add_argument(
        "--confirm-moved-collection",
        action="store_true",
        help=(
            "Bekreft at samme logiske bildesamling er flyttet til arbeidsstedet "
            "som vises av --dry-run"
        ),
    )
    snapshot_list = snapshot_subparsers.add_parser(
        "list",
        usage="bildebank snapshot list repository",
        description="Vis publiserte snapshots i et repository.",
    )
    snapshot_list.add_argument(
        "repository",
        metavar="repository",
        type=Path,
        help="Den eksakte repositorymappen",
    )
    snapshot_problems = snapshot_subparsers.add_parser(
        "problems",
        usage="bildebank snapshot problems repository [snapshot-id]",
        description="Vis kildeavvik og entry-ID-er fra publiserte snapshots.",
    )
    snapshot_problems.add_argument(
        "repository",
        metavar="repository",
        type=Path,
        help="Den eksakte repositorymappen",
    )
    snapshot_problems.add_argument(
        "snapshot_id",
        metavar="snapshot-id",
        nargs="?",
        help="Vis bare problemer fra dette snapshotet",
    )
    snapshot_check = snapshot_subparsers.add_parser(
        "check",
        usage="bildebank snapshot check repository [--full]",
        description="Kontroller snapshotmetadata og objekter uten å endre dem.",
    )
    snapshot_check.add_argument(
        "repository",
        metavar="repository",
        type=Path,
        help="Den eksakte repositorymappen",
    )
    snapshot_check.add_argument(
        "--full",
        action="store_true",
        help="Les og beregn SHA-256 for alle objekter, også urefererte",
    )
    snapshot_restore = snapshot_subparsers.add_parser(
        "restore",
        usage="bildebank snapshot restore repository snapshot-id ny-mappe [--dry-run]",
        description="Gjenopprett en hel bildesamling fra et snapshot.",
    )
    snapshot_restore.add_argument("repository", type=Path, help="Den eksakte repositorymappen")
    snapshot_restore.add_argument("snapshot_id", metavar="snapshot-id", help="Snapshot-ID som skal brukes")
    snapshot_restore.add_argument("destination", metavar="ny-mappe", type=Path, help="Ny eller tom målmappe")
    snapshot_restore.add_argument(
        "--dry-run",
        action="store_true",
        help="Vis en fullstendig plan uten å opprette eller kopiere filer",
    )
    snapshot_restore.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    snapshot_restore_file = snapshot_subparsers.add_parser(
        "restore-file",
        usage=(
            "bildebank snapshot restore-file repository snapshot-id eksportmappe "
            "(--path filsti | --entry-id id) [--variant expected|observed] [--dry-run]"
        ),
        description="Gjenopprett én fil fra et snapshot til en eksportmappe.",
    )
    snapshot_restore_file.add_argument("repository", type=Path, help="Den eksakte repositorymappen")
    snapshot_restore_file.add_argument("snapshot_id", metavar="snapshot-id", help="Snapshot-ID som skal brukes")
    snapshot_restore_file.add_argument("destination", metavar="eksportmappe", type=Path, help="Eksportmappe")
    restore_selection = snapshot_restore_file.add_mutually_exclusive_group(required=True)
    restore_selection.add_argument("--path", help="Normal relativ filsti i snapshotet")
    restore_selection.add_argument("--entry-id", help="Stabil entry-ID, også for recovery_only")
    snapshot_restore_file.add_argument(
        "--variant",
        choices=("expected", "observed"),
        help="Velg variant eksplisitt når både forventet og observert innhold finnes",
    )
    snapshot_restore_file.add_argument(
        "--dry-run",
        action="store_true",
        help="Vis eksportplanen uten å opprette eller kopiere filer",
    )
    snapshot_restore_file.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
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
    run_server_parser.add_argument(
        "--slideshow",
        action="store_true",
        help="Vis et automatisk slideshow read-only på privat LAN.",
    )
    run_server_parser.add_argument(
        "--delay",
        type=positive_int_arg,
        metavar="SEKUNDER",
        help=f"Sekunder per slideshow-bilde. Standard: {DEFAULT_SLIDESHOW_DELAY_SECONDS}",
    )
    run_server_parser.add_argument(
        "--filter",
        dest="slideshow_filter",
        metavar="UTTRYKK",
        help="Avgrens slideshowet med samme syntaks som Filtersøk.",
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
    ffmpeg_install = add_command(
        subparsers,
        "ffmpeg-install",
        usage="bildebank ffmpeg-install [valg]",
        help="Installer FFmpeg for videoavspilling",
        description="Last ned og installer FFmpeg og FFprobe i programmappen.",
    )
    ffmpeg_install.add_argument(
        "--force",
        action="store_true",
        help="Installer FFmpeg på nytt selv om riktig versjon allerede finnes.",
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
    lines = ["Kom i gang:"]
    lines.append("   bildebank start")
    lines.append("")
    lines.append(
        "Bildebank-vinduet kan opprette samling, importere bilder, starte nettleseren,"
    )
    lines.append("opprette snapshots og kjøre vanlig vedlikehold.")
    lines.append("")
    lines.append("Full kommandoliste:")
    lines.append("   docs\\reference.md")
    lines.append("   Noen avanserte oppgaver gjøres fortsatt fra PowerShell.")
    lines.append("")
    lines.append("Se hjelp for en kommando med:")
    lines.append("   bildebank <kommando> -h")
    return "\n".join(lines)


NO_TARGET_COMMANDS = {
    "start",
    "launcher",
    "create",
    "explain-date",
    "inspect-metadata",
    "update",
    "exiftool-install",
    "ffmpeg-install",
    "where-is",
    "doctor",
    "config",
    "download-face-model",
}

TARGET_COMMANDS = {
    "migrate",
    "snapshot",
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

    if args.command == "snapshot" and args.snapshot_command in {
        "list",
        "problems",
        "check",
        "restore",
        "restore-file",
    }:
        return run_snapshot_repository_command(args)

    target = resolve_target(
        args.target,
        allow_missing_main=(
            args.command == "snapshot" and args.snapshot_command == "create"
        ),
    )
    validate_collection_platform(target)
    if should_recover_pending_file_moves(args):
        recover_pending_file_moves(target)
    if args.command != "snapshot":
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
    if args.command in {"start", "launcher"}:
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

    if args.command == "ffmpeg-install":
        return run_ffmpeg_install(force=args.force)

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
    if args.command == "run-server" and (args.read_only or args.lan_share or args.slideshow):
        return False
    if getattr(args, "dry_run", False):
        return False
    if args.command == "snapshot":
        return False
    return True


def run_target_command(args: argparse.Namespace, target: Path) -> int:
    if args.command == "migrate":
        return run_migrate(target, check=args.check)

    if args.command == "snapshot":
        return run_snapshot_command(args, target)

    if args.command == "vacuum":
        return run_vacuum(target)

    if args.command in {"geo-scan", "geo-stats", "geo-areas", "geo-area"}:
        return run_geo_command(args, target, repo_root=program_repo_root())

    if args.command == "run-server":
        slideshow = args.slideshow
        lan_share = args.lan_share or slideshow
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
            slideshow_delay_seconds=(
                (args.delay if args.delay is not None else DEFAULT_SLIDESHOW_DELAY_SECONDS)
                if slideshow else None
            ),
            slideshow_filter=args.slideshow_filter if slideshow else None,
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

    def confirm_target_content_changes(changes: tuple[TargetContentChange, ...]) -> bool:
        print_unimport_target_content_warning(changes)
        answer = input('Fortsette unimport? Skriv "ja" eller "nei": ').strip().casefold()
        return answer == "ja"

    result = execute_unimport(
        target,
        args.name,
        config=load_config(program_repo_root()),
        dry_run=args.dry_run,
        confirm=confirm,
        confirm_target_content_changes=confirm_target_content_changes,
        source_progress=unimport_source_progress(),
        target_progress=unimport_target_progress(),
    )
    write_unimport_target_change_report(
        getattr(args, "target_change_report_json", None),
        result.target_content_changes,
    )
    if args.dry_run:
        print_unimport_plan(target, result.plan)
        if result.target_content_changes:
            print_unimport_target_content_warning(result.target_content_changes)
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
    if args.command == "make-video-previews":
        return run_make_video_previews_command(args, target)

    conn = db.connect(target)
    try:
        if not (args.command == "unimport" and args.dry_run):
            db.log_command(conn, args.command, vars_for_log(args))

        if args.command == "list-sources":
            for listed_source in db.get_sources(conn):
                imported = listed_source.imported_at or "-"
                print(
                    f"{listed_source.id}\t{listed_source.status}\t"
                    f"{imported}\t{listed_source.name}\t{listed_source.path}"
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
    return 0 if import_stats.errors == 0 and not import_stats.stopped else 2


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
    return 0 if rescan_stats.errors == 0 and not rescan_stats.stopped else 2


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


def run_make_video_previews_command(args: argparse.Namespace, target: Path) -> int:
    tools: FFmpegTools | None = None
    if not args.dry_run:
        tools = resolve_or_install_ffmpeg_tools()
    lock = nullcontext() if args.dry_run else TargetLock(target, command="make-video-previews")
    with lock:
        if not args.dry_run:
            conn = db.connect(target)
            try:
                db.log_command(conn, args.command, vars_for_log(args))
                conn.commit()
            finally:
                conn.close()
        stats = run_make_video_previews(
            target,
            tools,
            dry_run=args.dry_run,
            limit=args.limit,
            verbose=args.verbose,
            rebuild=args.rebuild,
            progress=print_video_preview_progress,
            target_locked=not args.dry_run,
        )
    print_video_preview_summary(stats, dry_run=args.dry_run)
    return 0 if stats.errors == 0 else 2


def resolve_or_install_ffmpeg_tools() -> FFmpegTools:
    repo_root = program_repo_root()
    try:
        return resolve_ffmpeg_tools(repo_root)
    except (FileNotFoundError, OSError, RuntimeError):
        if sys.platform != "win32":
            raise
    return install_managed_ffmpeg(repo_root).tools


def print_video_preview_summary(stats: VideoPreviewStats, *, dry_run: bool) -> None:
    missing = max(stats.checked - stats.skipped_current - stats.created, 0)
    prefix = "Videoavspillingskopier dry-run" if dry_run else "Videoavspillingskopier"
    print(
        f"{prefix}: totalt={stats.total}, sjekket={stats.checked}, "
        f"laget={stats.created}, ferske={stats.skipped_current}, mangler={missing}, feil={stats.errors}"
    )
    if stats.last_error_path is not None and stats.last_error_message:
        print(f"Siste feil: {stats.last_error_path}: {stats.last_error_message}")


def print_video_preview_progress(
    stage: str,
    current: int,
    total: int,
    stats: VideoPreviewStats,
    path: Path | None,
) -> None:
    global VIDEO_PREVIEW_PROGRESS
    if stage == "start":
        VIDEO_PREVIEW_PROGRESS = ProgressMeter("Videoavspillingskopier")
        VIDEO_PREVIEW_PROGRESS.message(
            f"Videoavspillingskopier: {total} AVI- og 3GP-filer skal kontrolleres."
        )
        return
    if VIDEO_PREVIEW_PROGRESS is None:
        VIDEO_PREVIEW_PROGRESS = ProgressMeter("Videoavspillingskopier")
    if stage == "error":
        message = stats.last_error_message or "ukjent feil"
        VIDEO_PREVIEW_PROGRESS.error(f"Videoavspillingskopi-feil: {path}\t{message}")
        return
    if stage == "check":
        VIDEO_PREVIEW_PROGRESS.update(
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
        VIDEO_PREVIEW_PROGRESS.done(
            f"Videoavspillingskopier: ferdig kontrollert {min(current, total)}/{total} filer."
        )
        VIDEO_PREVIEW_PROGRESS = None


def resolve_target(
    target_arg: Path | None,
    *,
    allow_missing_main: bool = False,
) -> Path:
    if target_arg is not None:
        target = target_arg.resolve()
        if not db.db_path_for_target(target).exists() and not (
            allow_missing_main and target.is_dir()
        ):
            raise ValueError(f"Bildesamlingen er ikke initialisert: {target}")
        return target
    found_target = db.find_target()
    if found_target is None:
        if allow_missing_main:
            current = Path.cwd().resolve()
            if current.is_dir():
                return current
        raise ValueError("Fant ingen bildesamling. Kjør kommandoen fra bildesamlingsmappen.")
    return found_target


def resolve_source_by_name(conn, name: str) -> db.Source:
    source = db.find_source_by_name(conn, name)
    if source is None:
        raise ValueError(f"Fant ikke kilde med navn: {name}")
    return source


def resolve_rescan_source(conn, name: str) -> db.Source:
    source = resolve_source_by_name(conn, name)
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
        return 0 if stats.errors == 0 and not stats.stopped else 2
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
    print(f"Registrerte originalfiler kontrollert: {plan.source_file_count}")
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


def print_unimport_target_content_warning(
    changes: tuple[TargetContentChange, ...],
) -> None:
    print("")
    print("ADVARSEL: fil(er) i bildesamlingen er endret siden import.")
    print(
        "Filene i kilden er verifisert, men disse filene matcher ikke lenger "
        "databaseført størrelse/SHA-256 og kan inneholde manuelle endringer:"
    )
    for change in changes:
        print(
            f"  {change.path.as_posix()} "
            f"(størrelse {change.actual_size_bytes}, forventet {change.expected_size_bytes})"
        )
    print("Hvis du fortsetter, kan disse endrede filene bli slettet.")


def write_unimport_target_change_report(
    report_path: Path | None,
    changes: tuple[TargetContentChange, ...],
) -> None:
    if report_path is None:
        return
    payload = {
        "changed_targets": [
            {
                "path": change.path.as_posix(),
                "expected_size_bytes": change.expected_size_bytes,
                "actual_size_bytes": change.actual_size_bytes,
                "expected_sha256": change.expected_sha256,
                "actual_sha256": change.actual_sha256,
            }
            for change in changes
        ]
    }
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


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


def run_ffmpeg_install(*, force: bool = False) -> int:
    if sys.platform != "win32":
        raise ValueError(
            "ffmpeg-install støttes bare på Windows. Installer FFmpeg med "
            "pakkesystemet slik at både ffmpeg og ffprobe finnes i PATH."
        )
    result = install_managed_ffmpeg(program_repo_root(), force=force)
    if result.installed:
        print(f"Installerte {result.tools.version}: {result.tools.ffmpeg.parent}")
    else:
        print(f"FFmpeg er allerede installert ({result.tools.version}): {result.tools.ffmpeg.parent}")
    return 0


def run_where_is() -> int:
    repo_root = program_repo_root()
    current_target = db.find_target()
    if current_target is not None:
        record_target_best_effort(repo_root, current_target)
    print("Bildebank-program:")
    print(f"  {repo_root}")
    print()
    print("Programdata:")
    print(f"  {program_db_path(repo_root)}")
    print()
    print("Gjeldende mappe:")
    print(f"  {Path.cwd().resolve()}")
    if current_target is not None:
        print("  bildesamling funnet:")
        print(f"  {current_target.resolve()}")
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


def run_snapshot_command(args: argparse.Namespace, target: Path) -> int:
    if args.snapshot_command != "create":
        raise ValueError(f"Ukjent snapshot-kommando: {args.snapshot_command}")
    if args.dry_run and args.confirm_moved_collection:
        raise ValueError(
            "--confirm-moved-collection kan ikke kombineres med --dry-run. "
            "Kjør først dry-run uten bekreftelsesvalget."
        )

    config = load_config(program_repo_root(), migrate_legacy=False)
    configured_face_dir = config.face_recognition.database_dir
    if not args.dry_run:
        confirmed_binding_change = None
        if args.confirm_moved_collection:
            print("Kontrollerer den flyttede repositorybindingen skrivefritt ...")
            confirmation_plan = plan_snapshot(
                target,
                args.repository,
                configured_face_database_dir=configured_face_dir,
                note=args.note,
            )
            confirmed_binding_change = confirmation_plan.binding_change
            if confirmed_binding_change is None:
                raise ValueError(
                    "Flyttebekreftelse ble angitt, men bildesamlingen har ikke flyttet "
                    "seg siden sist bekreftede snapshot."
                )
        meter = ProgressMeter("Snapshot")
        current_stage: list[str | None] = [None]

        def show_progress(progress: SnapshotCreateProgress) -> None:
            stage_changed = progress.stage != current_stage[0]
            if stage_changed:
                current_stage[0] = progress.stage
                meter.reset_eta()
            if progress.stage == "inventory":
                if progress.total_objects == 0:
                    meter.message("Snapshot: lager filinventar ...")
                elif stage_changed or progress.completed_objects >= progress.total_objects:
                    meter.message(
                        "Snapshot: filinventar="
                        f"{progress.completed_objects} filer ({format_bytes(progress.completed_bytes)})"
                    )
                return
            if progress.stage in {"files", "databases"}:
                object_label = "filer" if progress.stage == "files" else "databaser"
                details = (
                    f"{object_label}={progress.completed_objects}/{progress.total_objects}"
                )
                if progress.total_bytes > 0:
                    meter.update(
                        progress.completed_bytes,
                        progress.total_bytes,
                        action="byte",
                        details=details,
                        eta=True,
                        force=stage_changed
                        or progress.completed_bytes >= progress.total_bytes,
                    )
                else:
                    meter.update(
                        progress.completed_objects,
                        progress.total_objects,
                        action=object_label,
                        force=stage_changed
                        or progress.completed_objects >= progress.total_objects,
                    )
                return
            if progress.stage == "publish" and progress.completed_objects == 0:
                meter.message("Snapshot: publiserer manifest ...")

        try:
            result = create_snapshot(
                target,
                args.repository,
                face_config=config.face_recognition,
                note=args.note,
                confirmed_binding_change=confirmed_binding_change,
                progress=show_progress,
            )
        finally:
            meter.done()
        print_snapshot_creation_result(result)
        state_error = record_published_snapshot_best_effort(
            program_repo_root(),
            collection_id=result.build.collection_id,
            repository_id=result.build.repository_id,
            repository_path=result.repository,
            snapshot_id=result.published.snapshot_id,
            status=result.status,
        )
        if state_error is not None:
            print(
                "ADVARSEL: Snapshotet er publisert, men Bildebank klarte ikke å huske "
                f"repositoryet lokalt: {state_error}",
                file=sys.stderr,
            )
        return result.exit_code

    meter = ProgressMeter(
        "Snapshot dry-run",
        item_interval=1_000,
        time_interval_seconds=1.0,
    )
    plan_stage: list[str | None] = [None]

    def show_plan_progress(progress: SnapshotPlanProgress) -> None:
        stage_changed = progress.stage != plan_stage[0]
        if stage_changed:
            plan_stage[0] = progress.stage
            meter.reset_eta()
        if progress.stage == "database":
            meter.message("Snapshot dry-run: leser og kontrollerer hoveddatabasen ...")
            return
        if progress.stage == "database_complete":
            meter.message(
                f"Snapshot dry-run: hoveddatabase={progress.completed_objects} filposter"
            )
            return
        if progress.stage == "inventory":
            if progress.completed_objects == 0:
                meter.message("Snapshot dry-run: bygger filinventar ...")
            elif progress.total_objects == 0:
                meter.update_count(
                    progress.completed_objects,
                    action="filer funnet",
                    details=f"registrert={format_bytes(progress.completed_bytes)}",
                )
            else:
                meter.message(
                    "Snapshot dry-run: filinventar="
                    f"{progress.completed_objects} filer "
                    f"({format_bytes(progress.completed_bytes)})"
                )
            return
        if progress.stage == "files":
            if progress.total_objects == 0:
                meter.message("Snapshot dry-run: ingen databaseførte filer å sammenligne.")
            else:
                meter.update(
                    progress.completed_objects,
                    progress.total_objects,
                    action="databaseførte filer kontrollert",
                    eta=True,
                    force=stage_changed or progress.completed_objects >= progress.total_objects,
                )
            return
        if progress.stage == "storage":
            meter.message("Snapshot dry-run: beregner plassbehov ...")

    try:
        try:
            plan = plan_snapshot(
                target,
                args.repository,
                configured_face_database_dir=configured_face_dir,
                note=args.note,
                progress=show_plan_progress,
            )
        except MainDatabaseSourceError as exc:
            repository = validate_existing_recovery_repository(
                target,
                args.repository,
            )
            print_snapshot_recovery_plan(target, repository, str(exc))
            return 0
    finally:
        meter.done()
    print_snapshot_plan(plan)
    return 0


def run_snapshot_repository_command(args: argparse.Namespace) -> int:
    if args.snapshot_command == "list":
        list_result = list_repository_snapshots(args.repository)
        print_snapshot_list(list_result)
        return 0
    if args.snapshot_command == "problems":
        problems_result = list_snapshot_problems(args.repository, args.snapshot_id)
        print_snapshot_problems(problems_result)
        return 0
    if args.snapshot_command == "restore":
        full_restore_plan = plan_full_restore(args.repository, args.snapshot_id, args.destination)
        print_full_restore_plan(full_restore_plan, dry_run=args.dry_run)
        if args.dry_run:
            return 0
        confirmation = f"GJENOPPRETT {full_restore_plan.snapshot.snapshot_id}"
        if not args.yes:
            answer = input(
                "Skriv nøyaktig denne teksten for å starte restore:\n"
                f"  {confirmation}\n> "
            )
            if answer != confirmation:
                print("Restore avbrutt. Ingen samling ble publisert.", file=sys.stderr)
                return 1
        restore_result = restore_full_snapshot(
            args.repository,
            args.snapshot_id,
            args.destination,
        )
        print_full_restore_result(restore_result)
        return restore_result.exit_code
    if args.snapshot_command == "restore-file":
        file_restore_plan = plan_single_file_restore(
            args.repository,
            args.snapshot_id,
            args.destination,
            path=args.path,
            entry_id=args.entry_id,
            variant=args.variant,
        )
        print_single_file_restore_plan(file_restore_plan, dry_run=args.dry_run)
        if args.dry_run:
            return 0
        if not args.yes:
            answer = input("Eksportere filen som vist i planen? [j/N] ").strip().casefold()
            if answer not in {"j", "ja"}:
                print("Enkeltfil-restore avbrutt. Ingen fil ble eksportert.", file=sys.stderr)
                return 1
        file_restore_result = restore_single_file(
            args.repository,
            args.snapshot_id,
            args.destination,
            path=args.path,
            entry_id=args.entry_id,
            variant=args.variant,
        )
        print_single_file_restore_result(file_restore_result)
        return file_restore_result.exit_code
    if args.snapshot_command != "check":
        raise ValueError(f"Ukjent repositorykommando: {args.snapshot_command}")

    meter = ProgressMeter("Snapshot check") if args.full else None

    def show_progress(progress: SnapshotCheckProgress) -> None:
        assert meter is not None
        meter.update(
            progress.checked_bytes,
            progress.total_bytes,
            action="byte",
            details=f"objekter={progress.checked_objects}/{progress.total_objects}",
            eta=True,
            force=progress.checked_bytes == 0 or progress.checked_bytes >= progress.total_bytes,
        )

    result = check_snapshot_repository(
        args.repository,
        full=args.full,
        progress=show_progress if meter is not None else None,
    )
    if meter is not None:
        meter.done()
    print_snapshot_check_result(result)
    return result.exit_code


def print_snapshot_list(result: SnapshotCheckResult) -> None:
    print("Versjonerte snapshots")
    print(f"  Repository: {result.repository}")
    print(f"  Publiserte snapshots: {len(result.snapshots)}")
    for snapshot in result.snapshots:
        print()
        print(f"  {snapshot.completed_at}  {snapshot.snapshot_id}")
        print(f"    Status: {snapshot.status}")
        print(f"    Filposter: {snapshot.entry_count}")
        if snapshot.source_problem_count:
            print(f"    Kildeavvik: {snapshot.source_problem_count}")
        if snapshot.note is not None:
            print(f"    Kommentar: {snapshot.note}")
    for issue in result.issues:
        print(f"ADVARSEL: {issue.message}", file=sys.stderr)


def print_snapshot_problems(result: SnapshotProblemsResult) -> None:
    print("Problemer i versjonerte snapshots")
    print(f"  Repository: {result.repository}")
    print(f"  Kontrollerte snapshots: {len(result.snapshots)}")
    print(f"  Filavvik: {len(result.file_problems)}")
    print(f"  Databaseavvik: {len(result.database_problems)}")
    for problem in result.file_problems:
        entry = problem.entry
        print()
        print(f"  Snapshot-ID: {problem.snapshot.snapshot_id}")
        print(f"    Entry-ID: {entry.entry_id}")
        print(f"    Opprinnelig sti: {entry.original_path_display}")
        if entry.path is not None:
            print(f"    Snapshotsti: {entry.path}")
        if entry.recovery_name is not None:
            print(f"    Recovery-navn: {entry.recovery_name}")
        print(f"    Avvik: {entry.integrity_status}")
        print(f"    Restoretype: {entry.restore_kind}")
        variants = ", ".join(problem.recorded_variants) or "ingen lagrede byte"
        print(f"    Registrerte varianter: {variants}")
    for database_problem in result.database_problems:
        print()
        print(f"  Snapshot-ID: {database_problem.snapshot.snapshot_id}")
        print(f"    Database: {database_problem.role}")
        print(f"    Opprinnelig sti: {database_problem.source_path_display}")
        print(f"    Avvik: {database_problem.status}")
        print(f"    Sikring: {database_problem.capture}")


def print_snapshot_check_result(result: SnapshotCheckResult) -> None:
    title = "Full snapshotkontroll" if result.full else "Rask snapshotkontroll"
    state = "avbrutt" if result.cancelled else "fullført"
    print(f"{title} {state}")
    print(f"  Repository: {result.repository}")
    print(f"  Publiserte, lesbare snapshots: {len(result.snapshots)}")
    status_counts = {
        status: sum(snapshot.status == status for snapshot in result.snapshots)
        for status in ("complete", "degraded", "recovery")
    }
    print(
        "  Snapshotstatus: "
        f"complete={status_counts['complete']}, "
        f"degraded={status_counts['degraded']}, "
        f"recovery={status_counts['recovery']}"
    )
    print(f"  Refererte objekter: {result.referenced_objects}")
    print(f"  Urefererte objekter: {result.unreferenced_objects}")
    if result.full:
        print(f"  Fullhash-kontrollerte objekter: {result.checked_objects}/{result.total_objects}")
        print(f"  Fullhash-kontrollerte byte: {format_bytes(result.checked_bytes)}")
    print(f"  Ufullstendige kjøringer: {len(result.incomplete_runs)}")
    for run in result.incomplete_runs:
        print(
            f"ADVARSEL: Ufullstendig kjøring {run.run_id}: "
            f"{format_bytes(run.size_bytes)}, alder {format_duration_seconds(run.age_seconds)}",
            file=sys.stderr,
        )
    print(f"  Repositoryavvik: {len(result.issues)}")
    for issue in result.issues:
        print(f"FEIL: {issue.message}", file=sys.stderr)
        for affected in issue.affected:
            entry = f", entry_id={affected.entry_id}" if affected.entry_id is not None else ""
            print(
                f"  Berørt: snapshot={affected.snapshot_id}{entry}, sti={affected.logical_path}",
                file=sys.stderr,
            )
    if result.cancelled:
        print("ADVARSEL: Kontrollen ble avbrutt før hele repositoryet var kontrollert.", file=sys.stderr)


def format_duration_seconds(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, remaining_seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remaining_seconds:02d}s"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours}t {remaining_minutes:02d}m"


def print_full_restore_plan(plan: FullRestorePlan, *, dry_run: bool = True) -> None:
    target_state = "mangler og ville blitt opprettet" if plan.target_state == "missing" else "er tom"
    print("Hel restore dry-run" if dry_run else "Plan for hel restore")
    print(f"  Repository: {plan.repository}")
    print(f"  Snapshot-ID: {plan.snapshot.snapshot_id}")
    print(f"  Snapshotdato: {plan.snapshot.completed_at}")
    print(f"  Snapshotstatus: {plan.snapshot.status}")
    if plan.note is not None:
        print(f"  Kommentar: {plan.note}")
    print(f"  Målmappe: {plan.target} ({target_state})")
    print(f"  Ordinære utdatafiler: {len(plan.collection_outputs)}")
    print(f"  Recovery-filer: {len(plan.recovery_outputs)}")
    if plan.recovery_target is not None:
        print(f"  Recovery-mappe: {plan.recovery_target}")
    print(
        "  Forventede filer som mangler på ordinær plass: "
        f"{len(plan.missing_expected_entries)}"
    )
    for entry_id in plan.missing_expected_entries:
        print(
            f"ADVARSEL: Forventet fil mangler på ordinær plass for {entry_id}.",
            file=sys.stderr,
        )
    print(f"  Estimert datamengde: {format_bytes(plan.required_bytes)}")
    print(f"  Ledig plass: {format_bytes(plan.free_bytes)}")
    print(f"  Estimert plass er tilstrekkelig: {'ja' if plan.has_estimated_capacity else 'nei'}")
    if plan.original_collection_exists:
        print(
            "ADVARSEL: Den opprinnelige samlingen finnes fortsatt. Original og restore får samme "
            f"collection_id og må ikke brukes som uavhengige samlinger: {plan.original_collection}",
            file=sys.stderr,
        )
    for warning in plan.warnings:
        print(f"ADVARSEL: {warning}", file=sys.stderr)
    if dry_run:
        print("Dry-run: Ingen mapper, filer eller restore-staging er opprettet eller endret.")


def print_single_file_restore_plan(
    plan: SingleFileRestorePlan,
    *,
    dry_run: bool = True,
) -> None:
    export_state = "mangler og ville blitt opprettet" if plan.export_state == "missing" else "finnes"
    print("Enkeltfil-restore dry-run" if dry_run else "Plan for enkeltfil-restore")
    print(f"  Repository: {plan.repository}")
    print(f"  Snapshot-ID: {plan.snapshot.snapshot_id}")
    print(f"  Snapshotstatus: {plan.snapshot.status}")
    if plan.note is not None:
        print(f"  Kommentar: {plan.note}")
    print(f"  Entry-ID: {plan.output.entry_id}")
    print(f"  Opprinnelig sti: {plan.output.original_path_display}")
    print(f"  Variant: {plan.output.variant}")
    print(f"  Eksportmappe: {plan.export_directory} ({export_state})")
    print(f"  Utdatafil: {plan.output_path}")
    print(f"  Størrelse: {format_bytes(plan.output.object.size_bytes)}")
    if dry_run:
        print("Dry-run: Ingen mapper eller filer er opprettet eller endret.")


def print_single_file_restore_result(result: SingleFileRestoreResult) -> None:
    print("Enkeltfil-restore fullført")
    print(f"  Snapshot-ID: {result.plan.snapshot.snapshot_id}")
    print(f"  Entry-ID: {result.plan.output.entry_id}")
    print(f"  Variant: {result.plan.output.variant}")
    print(f"  Utdatafil: {result.output_path}")
    print(f"  Størrelse: {format_bytes(result.plan.output.object.size_bytes)}")


def print_full_restore_result(result: FullRestoreResult) -> None:
    print("Hel restore publisert")
    print(f"  Snapshot-ID: {result.plan.snapshot.snapshot_id}")
    print(f"  Samlingsmappe: {result.published_target}")
    print(f"  Ordinære utdatafiler: {len(result.plan.collection_outputs)}")
    if result.published_recovery is not None:
        print(f"  Recovery-mappe: {result.published_recovery}")
        print(f"  Recovery-filer: {len(result.plan.recovery_outputs)}")
    if result.plan.incomplete:
        print(
            "ADVARSEL: Samlingen ble publisert bevisst ufullstendig fordi forventede "
            "filer manglet på ordinær plass.",
            file=sys.stderr,
        )
    for warning in result.warnings:
        print(f"ADVARSEL: {warning}", file=sys.stderr)


def print_snapshot_creation_result(result: SnapshotCreationResult) -> None:
    status_text = {
        "complete": "fullført uten kjente avvik",
        "degraded": "opprettet med problemer",
        "recovery": "recovery-snapshot opprettet",
    }[result.status]
    print("Snapshot opprettet")
    print(f"  Status: {result.status} ({status_text})")
    print(f"  Snapshot-ID: {result.published.snapshot_id}")
    print(f"  Snapshotmappe: {result.published.snapshot_dir}")
    print(f"  Filposter: {result.published.entry_count}")
    print(f"  Repository: {result.repository}")
    if result.repository_initialized:
        print("  Repositoryet ble initialisert")
    for warning in result.build.warnings:
        print(f"ADVARSEL: {warning}", file=sys.stderr)


def print_snapshot_plan(plan: SnapshotPlan) -> None:
    inventory = plan.inventory
    storage = plan.storage
    state_text = {
        "missing": "Mangler; ville blitt opprettet og initialisert",
        "empty": "Finnes og er tom; ville blitt initialisert",
        "existing": "Eksisterende repository med gyldig metadata",
    }.get(plan.repository_state, plan.repository_state)

    print("Snapshot dry-run")
    print(f"  Bildesamling: {plan.source_dir}")
    print(f"  Repository: {plan.repository_dir}")
    print(f"  Repositorytilstand: {state_text}")
    print(f"  collection_id: {plan.collection_id}")
    if plan.note is not None:
        print(f"  Kommentar: {plan.note}")
    if plan.binding_change is not None:
        change = plan.binding_change
        print()
        print("Flytting må bekreftes før snapshotet kan opprettes")
        print("  Sist bekreftede arbeidssted:")
        print(f"    Maskin: {change.previous_machine_name}")
        print(f"    Samlingssti: {change.previous_collection_path}")
        print("  Nåværende arbeidssted:")
        print(f"    Maskin: {change.current_machine_name}")
        print(f"    Samlingssti: {change.current_collection_path}")
        print(
            "  Fortsett bare hvis dette er samme logiske samling som er flyttet, "
            "ikke to uavhengige kopier."
        )
        print(
            "  Kjør den reelle kommandoen med --confirm-moved-collection "
            "for å bekrefte flyttingen."
        )
    print()
    print("Inventar")
    print(f"  Alle filer: {inventory.total_files} ({format_bytes(inventory.total_bytes)})")
    print(f"  Ekskludert: {inventory.excluded_files} ({format_bytes(inventory.excluded_bytes)})")
    exclusion_labels = {
        "thumbnails": "thumbnails",
        "generated_html": "generert HTML",
        "runtime": "runtime-filer",
    }
    for exclusion in inventory.exclusions:
        label = exclusion_labels.get(exclusion.reason, exclusion.reason)
        print(f"    {label}: {exclusion.files} ({format_bytes(exclusion.bytes)})")
    print(
        "  Databaselagring: "
        f"{inventory.database_storage_files} ({format_bytes(inventory.database_storage_bytes)})"
    )
    print(f"  SQLite-sidefiler: {inventory.database_side_files}")
    print(f"  Databaseførte mediefiler: {inventory.database_files}")
    print(f"    størrelse stemmer: {inventory.matched_database_files}")
    print(f"    mangler: {inventory.missing_database_files}")
    print(f"    feil størrelse: {inventory.wrong_size_database_files}")
    print(f"    ugyldig portabel sti: {inventory.invalid_database_paths}")
    print(
        "  Bildebank-migreringsbackuper: "
        f"{inventory.migration_backup_files} ({format_bytes(inventory.migration_backup_bytes)})"
    )
    print(f"  Ukjente filer: {inventory.unknown_files} ({format_bytes(inventory.unknown_bytes)})")
    print(f"  Planlagt recovery_only: {inventory.recovery_only_files}")
    print(f"  Windows-stikollisjoner: {inventory.path_collisions}")
    print()
    print("Konservativt plassestimat")
    print(f"  Gjenbrukbare objekter: {storage.reusable_objects}")
    print(f"  Estimerte nye objekter: {storage.estimated_new_objects}")
    print(f"  Estimerte nye byte: {format_bytes(storage.estimated_new_bytes)}")
    print(f"  Ledig plass: {format_bytes(storage.free_bytes)}")
    print(f"  Estimert plass er tilstrekkelig: {'ja' if storage.has_estimated_capacity else 'nei'}")
    for warning in plan.warnings:
        print(f"ADVARSEL: {warning}", file=sys.stderr)
    print()
    print("Dry-run: Ingen filer, metadata, mapper eller låser er opprettet eller endret.")


def print_snapshot_recovery_plan(
    source: Path,
    repository: Path,
    database_error: str,
) -> None:
    print("Plan for recovery-snapshot")
    print(f"  Bildesamling: {source}")
    print(f"  Repository: {repository}")
    print("  Hoveddatabasen kunne ikke valideres normalt.")
    print(f"  Databasefeil: {database_error}")
    print("  Repositorybindingen er kontrollert skrivefritt.")
    print("  Normal plassberegning er ikke mulig uten en gyldig hoveddatabase.")
    print(
        "  Reell kjøring vil sikre lesbare filer og rå databaser som recovery-data."
    )
    print()
    print("Dry-run: Ingen filer, metadata, mapper eller låser er opprettet eller endret.")


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
        )
        stats = import_source_dry_run(
            conn, target, source_row, output=sys.stdout, verbose=not args.quiet
        )
        print_summary(stats)
        return 0 if stats.errors == 0 and not stats.stopped else 2
    finally:
        conn.close()


def run_migrate(target: Path, *, check: bool) -> int:
    plan = db.migration_plan(target, validate=check)
    if not check and plan.current_version == plan.target_version:
        plan = db.migration_plan(target, validate=True)
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
    if plan.adds_pending_file_delete_identity:
        print("  legge til innholdsidentitet for pending_file_deletes")
    if plan.creates_pending_file_moves:
        print("  opprette pending_file_moves")
    if plan.adds_metadata_datetime_column:
        print("  legge til metadata_datetime i files")
    if plan.adds_comment_column:
        print("  legge til comment i files")
    if plan.removes_superseded_sources:
        print("  fjerne gammel superseded-kildemodell")
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
    if result.adds_pending_file_delete_identity:
        print("Legger til innholdsidentitet for pending_file_deletes.")
    if result.creates_pending_file_moves:
        print("Oppretter pending_file_moves.")
    if result.adds_metadata_datetime_column:
        print("Legger til metadata_datetime i files.")
        print("Kjør bildebank refresh-metadata --rescan for å fylle tidspunkt for eksisterende filer.")
    if result.adds_comment_column:
        print("Legger til comment i files.")
    if result.removes_superseded_sources:
        print("Fjerner gammel superseded-kildemodell.")
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
    print(f"  finnes i kilden: {'ja' if source_path.exists() else 'nei'}")


def print_source_item(target: Path, row) -> None:
    source_path = Path(str(row["source_path"]))
    source_label = row["source_name"] or row["source_root"]
    print(f"Importert fil: {db.absolute_target_path(target, Path(str(row['target_path'])))}")
    print(f"Fil i kilde: {source_path}")
    print(f"Finnes i kilden: {'ja' if source_path.exists() else 'nei'}")
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
    print(f"Importert fil: {db.absolute_target_path(target, Path(str(first['target_path'])))}")
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
    print("Filer i kilder:")
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
    print(f"  fil i kilde: {row['source_path']}")
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
    stopped = ", avbrutt=ja" if stats.stopped else ""
    return (
        "Oppsummering: "
        f"scannet={stats.scanned}, importert={stats.imported}, "
        f"duplikater={stats.duplicates}, eksisterende={stats.skipped_existing}, "
        f"navnekollisjoner={stats.name_conflicts}, feil={stats.errors}{stopped}"
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
    print(f"  Registrerte filer i kilder: {db.count_rows(conn, 'file_sources')}")
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
