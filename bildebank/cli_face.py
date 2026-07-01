from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path

from . import db
from .config import CONFIG_FILENAME, load_config
from .export_person import export_person
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
    delete_face_database,
    delete_person,
    export_people_browser,
    export_person_browser,
    face_db_path,
    face_report,
    list_persons,
    remove_face_from_person,
    rename_person,
    reset_face_database,
    scan_faces,
    suggest_faces,
)
from .progress import ProgressMeter
from .target_lock import TargetLock


FACE_SCAN_PROGRESS: ProgressMeter | None = None
FACE_SUGGEST_PROGRESS: ProgressMeter | None = None


def run_download_face_model(repo_root: Path) -> int:
    from .face import insightface_model_files_exist, load_face_app

    config = load_config(repo_root).face_recognition
    print(f"InsightFace-modell: {config.model_name}")
    print(f"Modellmappe: {config.model_root}")
    if insightface_model_files_exist(config):
        print("Modellen er allerede lastet ned.")
        return 0

    print("Laster ned og klargjør modellen. Første kjøring kan ta lang tid.")
    load_face_app(config)
    if not insightface_model_files_exist(config):
        raise ValueError(f"InsightFace-modellen {config.model_name!r} ble ikke funnet etter nedlasting.")
    print("Modellen er lastet ned.")
    return 0


def run_face_command(args: argparse.Namespace, target: Path, *, repo_root: Path) -> int:
    require_face_enabled(load_config(repo_root).face_recognition.enabled)

    if args.command == "face-scan":
        return run_face_scan(
            target,
            repo_root=repo_root,
            limit=args.limit,
            force=args.force,
            show_model_output=args.show_model_output,
        )

    if args.command == "face-report":
        return run_face_report(target, repo_root=repo_root, limit=args.limit)

    if args.command == "face-person-create":
        config = load_config(repo_root).face_recognition
        person_id = create_person(target, args.name, config)
        print(f"Person #{person_id}: {args.name.strip()}")
        return 0

    if args.command == "face-person-add-face":
        config = load_config(repo_root).face_recognition
        add_result = add_face_to_person(target, args.name, args.face_id, config)
        print_add_face_to_person_result(add_result)
        return 0

    if args.command == "face-person-remove-face":
        config = load_config(repo_root).face_recognition
        remove_result = remove_face_from_person(target, args.name, args.face_id, config)
        print_remove_face_from_person_result(remove_result)
        return 0

    if args.command == "face-person-delete":
        return run_face_person_delete(target, args.name, repo_root=repo_root)

    if args.command == "face-person-rename":
        config = load_config(repo_root).face_recognition
        rename_result = rename_person(target, args.old_name, args.new_name, config)
        print_rename_person_result(rename_result)
        return 0

    if args.command == "face-person-list":
        print_persons(target, repo_root=repo_root)
        return 0

    if args.command == "export-person":
        app_config = load_config(repo_root)
        plan = export_person(
            target,
            args.name,
            args.dest,
            config=app_config,
            dry_run=args.dry_run,
        )
        if args.dry_run:
            for entry in plan.entries:
                print(f"{entry.source} -> {entry.destination}")
        print(f"Antall bilder: {len(plan.entries)}")
        if not args.dry_run:
            print(f"Eksportert til: {plan.destination}")
            print(f"Statisk browser: {plan.destination / 'index.html'}")
        return 0

    if args.command == "face-suggest":
        return run_face_suggest(target, repo_root=repo_root, threshold=args.threshold, model=args.model)

    if args.command == "make-person-browser":
        output = args.output.resolve() if args.output else None
        with TargetLock(target, command="make-person-browser"):
            output_path = export_person_browser(
                target,
                args.name,
                output,
                month_preview_limit=args.month_preview_limit,
                hide_out_of_focus=args.hide_out_of_focus,
                config=load_config(repo_root).face_recognition,
                target_locked=True,
            )
        print(f"Skrev HTML-browser for person: {output_path}")
        return 0

    if args.command == "make-people-browser":
        with TargetLock(target, command="make-people-browser"):
            browser_result = export_people_browser(
                target,
                month_preview_limit=args.month_preview_limit,
                hide_out_of_focus=args.hide_out_of_focus,
                config=load_config(repo_root).face_recognition,
                target_locked=True,
            )
        print(f"Skrev person-index: {browser_result.index_path}")
        print(f"Skrev personsider: {len(browser_result.person_pages)}")
        return 0

    if args.command == "face-reset":
        return run_face_reset(
            target,
            repo_root=repo_root,
            all_data=args.all,
        )

    raise ValueError(f"Ukjent ansiktskommando: {args.command}")


def run_face_scan(
    target: Path,
    *,
    repo_root: Path,
    limit: int | None,
    force: bool = False,
    show_model_output: bool = False,
) -> int:
    config = load_config(repo_root).face_recognition
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


def run_face_report(target: Path, *, repo_root: Path, limit: int) -> int:
    config = load_config(repo_root).face_recognition
    report = face_report(target, limit=limit, config=config)
    print_face_report(target, report, config=config)
    return 0


def run_face_suggest(target: Path, *, repo_root: Path, threshold: float, model: str | None = None) -> int:
    config = load_config(repo_root).face_recognition
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


def run_face_person_delete(target: Path, name: str, *, repo_root: Path) -> int:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Personnavn kan ikke være tomt.")
    answer = input(f'Skriv "slett {clean_name}" for å slette personen fra ansiktsdatabasen: ')
    if answer != f"slett {clean_name}":
        print("Avbrutt. Ingen endringer er gjort.")
        return 0
    config = load_config(repo_root).face_recognition
    result = delete_person(target, clean_name, config)
    print_delete_person_result(result)
    return 0


def print_delete_person_result(result: DeletePersonResult) -> None:
    print(f"Slettet person: {result.person_name}")
    print(f"Fjernet bekreftede ansiktskoblinger: {result.removed_faces}")
    print(f"Fjernet manuelle person-i-bilde-koblinger: {result.removed_files}")
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


def print_persons(target: Path, *, repo_root: Path) -> None:
    config = load_config(repo_root).face_recognition
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
            target_path = db.relative_path(Path(str(row["target_path"])))
            print(f"  {row['face_count']}\t{target_path.as_posix()}")
    if report.errors:
        print()
        print("Siste scan-feil:")
        for row in report.errors:
            target_path = db.relative_path(Path(str(row["target_path"])))
            print(f"  {target_path.as_posix()}\t{row['error_message']}")


def run_face_reset(
    target: Path,
    *,
    repo_root: Path,
    all_data: bool = False,
) -> int:
    config = load_config(repo_root).face_recognition
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
        deleted_path = delete_face_database(target, config)
        if deleted_path is None:
            print(f"Fant ingen face-database: {path}")
            return 0
        print(f"Slettet face-database: {deleted_path}")
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
    print(f"Slettet manuelle person-i-bilde-koblinger: {result.removed_person_files}")
    print(f"Slettet ansiktsforslag: {result.removed_suggestions}")


def require_face_enabled(enabled: bool) -> None:
    if not enabled:
        raise ValueError(
            f"Ansiktsgjenkjenning er av. Sett enabled = true i {CONFIG_FILENAME} "
            "hvis du vil teste."
        )
