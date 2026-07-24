from __future__ import annotations

import importlib.util
import os
import re
import sqlite3
import stat
import sys
from pathlib import Path

from . import db
from .collection_paths import (
    COLLECTION_FILE_MISSING,
    COLLECTION_FILE_NOT_REGULAR,
    COLLECTION_FILE_OK,
    CollectionFileHashError,
    InvalidCollectionRelativePath,
    hash_stable_collection_file,
    inspect_collection_file,
    inspect_existing_collection_path_components,
    is_active_collection_file_path,
    is_deleted_collection_file_path,
    is_reparse_stat,
    parse_collection_relative_path,
)
from .config import CONFIG_FILENAME, load_config
from .db_core import connect_database_read_only
from .exiftool import resolve_exiftool_path, validate_exiftool_install
from .face import face_db_path, face_db_summary, insightface_runtime_error
from .ffmpeg_tools import resolve_ffmpeg_tools
from .media import is_supported_media, sha256_file
from .openclip import openclip_db_path, openclip_db_summary, torch_gpu_status
from .pending_deletes import pending_delete_integrity_rows
from .platform_guard import validate_collection_platform
from .progress import ProgressMeter


def run_doctor(target_arg: Path | None = None, *, deep: bool = False, repo_root: Path) -> int:
    target = db.find_target(target_arg)
    if target is not None:
        validate_collection_platform(target)

    config_path = repo_root / CONFIG_FILENAME
    config = load_config(repo_root, migrate_legacy=False)
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

    try:
        ffmpeg_tools = resolve_ffmpeg_tools(repo_root)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        doctor_error(f"FFmpeg mangler eller virker ikke: {exc}")
        if sys.platform == "win32":
            doctor_advice("Kjør `bildebank ffmpeg-install` fra programmappen.")
        else:
            doctor_advice("Installer både FFmpeg og FFprobe med pakkesystemet.")
    else:
        doctor_ok(f"FFmpeg funnet: {ffmpeg_tools.ffmpeg} ({ffmpeg_tools.version})")

    print()
    print("Ansiktsgjenkjenning:")
    if face.enabled:
        doctor_ok(f"face_recognition er slått på ({face.model_name}, {face.provider})")
        insightface_error = insightface_runtime_error()
        if insightface_error is None:
            doctor_ok("insightface installert")
        else:
            doctor_error(insightface_error)
            if "libGL.so.1" in insightface_error:
                doctor_advice("Installer Linux-pakken: `sudo apt install libgl1`.")
            else:
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
        print()
        print("Databaseintegritet:")
        if doctor_check_main_database_health(target):
            file_paths_safe = doctor_check_file_paths(target)
            if doctor_check_current_schema(target):
                doctor_check_pending_file_moves(target)
                doctor_check_pending_file_deletes(target)
                doctor_check_duplicate_active_sha256(target)
                doctor_check_files_have_sources(target)
                doctor_check_file_source_identity(target)
                doctor_check_orphan_openclip_rows(target)
                if file_paths_safe:
                    doctor_check_files_on_disk(target)
                    doctor_check_orphan_files(target)
                    if deep:
                        print()
                        print("Dyp filintegritet:")
                        doctor_deep_check_active_file_hashes(target)
                else:
                    doctor_obs(
                        "filkontroller er hoppet over fordi databaseførte "
                        "samlingsstier ikke er bekreftet som trygge."
                    )
            else:
                doctor_obs(
                    "øvrige database- og filkontroller er hoppet over fordi "
                    "gjeldende databaseschema ikke er bekreftet."
                )
        else:
            doctor_obs(
                "øvrige database- og filkontroller er hoppet over fordi "
                "hoveddatabasens integritet ikke er bekreftet."
            )
    else:
        print()
        print("Aktiv bildesamling:")
        doctor_obs("ingen aktiv bildesamling funnet.")
        doctor_advice("Kjør kommandoen fra en bildesamling, eller bruk `--target`.")
        doctor_advice('Eksempel: "bildebank --target C:\\bildesamling doctor"')
    return 0


def doctor_check_main_database_health(target: Path) -> bool:
    try:
        conn = db.connect_read_only(target, require_current=False)
    except sqlite3.Error as exc:
        doctor_error(f"kunne ikke åpne hoveddatabasen read-only: {exc}")
        doctor_advice("Undersøk databasen og sikkerhetskopien før du gjør endringer.")
        return False

    integrity_errors: list[str] | None = None
    foreign_key_errors: list[sqlite3.Row] | None = None
    try:
        try:
            integrity_errors = db.database_integrity_errors(conn)
        except sqlite3.Error as exc:
            doctor_error(f"SQLite integrity_check kunne ikke kjøres: {exc}")

        try:
            foreign_key_errors = db.database_foreign_key_errors(conn)
        except sqlite3.Error as exc:
            doctor_error(f"SQLite foreign_key_check kunne ikke kjøres: {exc}")
    finally:
        conn.close()

    if integrity_errors is not None:
        if integrity_errors:
            doctor_error(
                f"SQLite integrity_check fant {len(integrity_errors)} feil."
            )
            for error in integrity_errors[:20]:
                doctor_info(error)
            doctor_report_omitted_details(len(integrity_errors))
        else:
            doctor_ok("SQLite integrity_check: ok")

    if foreign_key_errors is not None:
        if foreign_key_errors:
            reference_label = (
                "ugyldig referanse"
                if len(foreign_key_errors) == 1
                else "ugyldige referanser"
            )
            doctor_error(
                "SQLite foreign_key_check fant "
                f"{len(foreign_key_errors)} {reference_label}."
            )
            for row in foreign_key_errors[:20]:
                doctor_info(
                    f"table={row['table']} rowid={row['rowid']} "
                    f"parent={row['parent']} foreign_key={row['fkid']}"
                )
            doctor_report_omitted_details(len(foreign_key_errors))
        else:
            doctor_ok("SQLite foreign_key_check: ingen feil")

    can_continue = (
        integrity_errors == []
        and foreign_key_errors is not None
    )
    if not can_continue or foreign_key_errors:
        doctor_advice("Undersøk databasen og sikkerhetskopien før du gjør endringer.")
    return can_continue


def doctor_check_current_schema(target: Path) -> bool:
    try:
        conn = db.connect_read_only(target)
    except (sqlite3.Error, ValueError) as exc:
        doctor_error(f"gjeldende databaseschema kunne ikke bekreftes: {exc}")
        doctor_advice("Undersøk databasen og sikkerhetskopien før du gjør endringer.")
        return False
    conn.close()
    doctor_ok(f"databaseschema v{db.SCHEMA_VERSION}: ok")
    return True


def doctor_check_file_paths(target: Path) -> bool:
    try:
        conn = db.connect_read_only(target, require_current=False)
    except sqlite3.Error as exc:
        doctor_error(f"databaseførte samlingsstier kunne ikke leses: {exc}")
        return False

    try:
        rows = db.file_path_integrity_rows(conn)
        database_issues = db.file_path_integrity_issues(conn)
    except sqlite3.Error as exc:
        doctor_error(f"databaseførte samlingsstier kunne ikke kontrolleres: {exc}")
        return False
    finally:
        conn.close()

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

    if not database_issues and not component_issues:
        doctor_ok(
            "databaseførte samlingsstier, mappeplasseringer og "
            "target_path_key er gyldige"
        )
        return True

    if database_issues:
        affected_files = len({issue.file_id for issue in database_issues})
        doctor_error(
            f"{len(database_issues)} databaseført(e) stifeil i "
            f"{affected_files} files-rad(er)."
        )
        for issue in database_issues[:20]:
            doctor_info(
                f"file #{issue.file_id} {issue.field}={issue.value!r}: "
                f"{issue.message}"
            )
        doctor_report_omitted_details(len(database_issues))

    if component_issues:
        doctor_error(
            f"{len(component_issues)} databaseført(e) sti(er) går gjennom "
            "en usikker stikomponent."
        )
        for file_id, field, value, component, reason in component_issues[:20]:
            doctor_info(
                f"file #{file_id} {field}={value!r}: "
                f"{component} ({reason})"
            )
        doctor_report_omitted_details(len(component_issues))

    doctor_advice(
        "Ikke åpne, flytt eller hash databaseførte filer før stifeilene er "
        "undersøkt mot sikkerhetskopien."
    )
    return False


def doctor_check_duplicate_active_sha256(target: Path) -> None:
    conn = db.connect_read_only(target)
    try:
        rows = db.duplicate_active_sha256_files(conn)
    finally:
        conn.close()

    if not rows:
        doctor_ok("ingen duplikate aktive SHA-256-verdier i files")
        return

    duplicate_hash_count = len({str(row["sha256"]) for row in rows})
    doctor_error(
        f"{duplicate_hash_count} SHA-256-verdi(er) finnes på flere aktive filer."
    )
    doctor_advice(
        "Ikke legg på UNIQUE-index for aktive files.sha256 før dette er ryddet."
    )

    current_sha256 = None
    for row in rows:
        sha256 = str(row["sha256"])
        if sha256 != current_sha256:
            current_sha256 = sha256
            doctor_info(
                f"sha256={sha256} ({int(row['duplicate_count'])} aktive filer)"
            )
        doctor_info(
            f"  file #{int(row['id'])}: "
            f"{Path(str(row['target_path'])).as_posix()}"
        )


def doctor_check_files_have_sources(target: Path) -> None:
    conn = db.connect_read_only(target)
    try:
        rows = db.files_without_sources(conn)
    finally:
        conn.close()

    if not rows:
        doctor_ok("alle files-rader har minst én file_sources-rad")
        return

    active_count = sum(row["deleted_at"] is None for row in rows)
    deleted_count = len(rows) - active_count
    doctor_error(
        f"{len(rows)} files-rad(er) mangler file_sources-proveniens "
        f"(aktive={active_count}, slettede={deleted_count})."
    )
    for row in rows[:20]:
        state = "slettet" if row["deleted_at"] is not None else "aktiv"
        doctor_info(
            f"file #{int(row['id'])} ({state}): "
            f"{Path(str(row['target_path'])).as_posix()}"
        )
    doctor_report_omitted_details(len(rows))
    doctor_advice("Undersøk importfeilen og sikkerhetskopien før databasen endres.")


def doctor_check_file_source_identity(target: Path) -> None:
    conn = db.connect_read_only(target)
    try:
        rows = db.file_source_integrity_mismatches(conn)
    finally:
        conn.close()

    if not rows:
        doctor_ok("SHA-256 og størrelse stemmer mellom files og file_sources")
        return

    hash_mismatches = [
        row for row in rows if str(row["source_sha256"]) != str(row["file_sha256"])
    ]
    size_mismatches = [
        row
        for row in rows
        if row["source_size_bytes"] != row["file_size_bytes"]
    ]

    if hash_mismatches:
        doctor_error(
            f"{len(hash_mismatches)} file_sources-rad(er) har SHA-256 "
            "som ikke stemmer med files."
        )
        for row in hash_mismatches[:20]:
            doctor_file_source_mismatch_info(
                row,
                field="sha256",
                file_value=str(row["file_sha256"]),
                source_value=str(row["source_sha256"]),
            )
        doctor_report_omitted_details(len(hash_mismatches))

    if size_mismatches:
        doctor_error(
            f"{len(size_mismatches)} file_sources-rad(er) har størrelse "
            "som ikke stemmer med files."
        )
        for row in size_mismatches[:20]:
            doctor_file_source_mismatch_info(
                row,
                field="size_bytes",
                file_value=str(row["file_size_bytes"]),
                source_value=str(row["source_size_bytes"]),
            )
        doctor_report_omitted_details(len(size_mismatches))

    doctor_advice("Undersøk importen og sikkerhetskopien før databasen endres.")


def doctor_file_source_mismatch_info(
    row: sqlite3.Row,
    *,
    field: str,
    file_value: str,
    source_value: str,
) -> None:
    state = "slettet" if row["deleted_at"] is not None else "aktiv"
    target_path = Path(str(row["target_path"])).as_posix()
    doctor_info(
        f"file_sources #{int(row['file_source_id'])} -> "
        f"file #{int(row['file_id'])} ({state}): {target_path} "
        f"(files.{field}={file_value}, file_sources.{field}={source_value})"
    )


def doctor_check_pending_file_moves(target: Path) -> None:
    conn = db.connect_read_only(target)
    try:
        rows = db.prepared_pending_file_moves(conn)
    finally:
        conn.close()

    if not rows:
        doctor_ok("ingen uavklarte filflyttinger")
        return
    doctor_error(f"{len(rows)} uavklarte filflytting(er) i pending_file_moves.")
    for row in rows[:20]:
        doctor_info(
            f"pending_file_moves #{row['id']}: {row['operation']} "
            f"{row['from_path']} -> {row['to_path']}"
        )
    doctor_report_omitted_details(len(rows))
    doctor_advice("Kjør en Bildebank-kommando på nytt etter at filtilstanden er rettet.")


def doctor_check_pending_file_deletes(target: Path) -> None:
    conn = db.connect_read_only(target)
    try:
        rows = pending_delete_integrity_rows(conn)
        referenced_path_keys = db.file_target_path_keys(conn)
    finally:
        conn.close()

    if not rows:
        doctor_ok("ingen filer i pending_file_deletes")
        return

    ready: list[tuple[int, str, str, int, str | None]] = []
    missing: list[tuple[int, str, str, int, str | None]] = []
    errors: list[tuple[int, object, str]] = []
    progress = ProgressMeter("Doctor pending deletes")
    progress.update(0, len(rows), action="kontrollert", force=True)

    for current, row in enumerate(rows, start=1):
        pending_id = int(row["id"])
        raw_path = row["path"]
        reason_value = row["reason"]
        attempts_value = row["attempts"]
        if (
            not isinstance(reason_value, str)
            or not reason_value.strip()
            or type(attempts_value) is not int
            or attempts_value < 0
        ):
            errors.append(
                (
                    pending_id,
                    raw_path,
                    "køposten har ugyldig årsak eller antall forsøk",
                )
            )
            progress.update(current, len(rows), action="kontrollert")
            continue
        reason = reason_value
        attempts = attempts_value
        last_error = (
            str(row["last_error"])
            if row["last_error"] is not None
            else None
        )
        try:
            relative_path = parse_collection_relative_path(raw_path)
        except InvalidCollectionRelativePath as exc:
            errors.append(
                (pending_id, raw_path, f"ugyldig køsti: {exc}")
            )
            progress.update(current, len(rows), action="kontrollert")
            continue

        if not (
            is_active_collection_file_path(relative_path)
            or is_deleted_collection_file_path(relative_path)
        ):
            errors.append(
                (
                    pending_id,
                    raw_path,
                    "køstien har ikke en gyldig års-/måneds-, "
                    "udatert/- eller deleted/-layout",
                )
            )
            progress.update(current, len(rows), action="kontrollert")
            continue

        expected_sha256 = row["expected_sha256"]
        expected_size = row["expected_size_bytes"]
        if (
            not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
            or type(expected_size) is not int
            or expected_size < 0
        ):
            errors.append(
                (
                    pending_id,
                    raw_path,
                    "mangler gyldig forventet SHA-256 og størrelse",
                )
            )
            progress.update(current, len(rows), action="kontrollert")
            continue

        path_key = db.relative_path_key(relative_path)
        if path_key in referenced_path_keys:
            errors.append(
                (
                    pending_id,
                    raw_path,
                    "filen har fortsatt en referanse i files",
                )
            )
            progress.update(current, len(rows), action="kontrollert")
            continue

        inspection = inspect_collection_file(target, relative_path)
        if inspection.status == COLLECTION_FILE_MISSING:
            missing.append(
                (
                    pending_id,
                    relative_path.as_posix(),
                    reason,
                    attempts,
                    last_error,
                )
            )
            progress.update(current, len(rows), action="kontrollert")
            continue
        if inspection.status != COLLECTION_FILE_OK:
            errors.append(
                (
                    pending_id,
                    raw_path,
                    inspection.message
                    or "køstien er ikke en vanlig, trygg fil",
                )
            )
            progress.update(current, len(rows), action="kontrollert")
            continue
        if inspection.size_bytes != expected_size:
            errors.append(
                (
                    pending_id,
                    raw_path,
                    "filstørrelsen stemmer ikke "
                    f"(forventet={expected_size}, disk={inspection.size_bytes})",
                )
            )
            progress.update(current, len(rows), action="kontrollert")
            continue

        try:
            actual_sha256, actual_size = hash_stable_collection_file(
                target,
                relative_path,
            )
        except (CollectionFileHashError, OSError) as exc:
            errors.append(
                (
                    pending_id,
                    raw_path,
                    f"filen kunne ikke hashes stabilt: {exc}",
                )
            )
        else:
            if actual_size != expected_size:
                errors.append(
                    (
                        pending_id,
                        raw_path,
                        "filstørrelsen endret seg under hashing "
                        f"(forventet={expected_size}, lest={actual_size})",
                    )
                )
            elif actual_sha256 != expected_sha256:
                errors.append(
                    (
                        pending_id,
                        raw_path,
                        "SHA-256 stemmer ikke med forventet innhold",
                    )
                )
            else:
                ready.append(
                    (
                        pending_id,
                        relative_path.as_posix(),
                        reason,
                        attempts,
                        last_error,
                    )
                )
        progress.update(current, len(rows), action="kontrollert")

    progress.done(
        "Doctor pending deletes: ferdig kontrollert "
        f"{len(rows)}/{len(rows)} køposter."
    )

    if errors:
        doctor_error(
            f"{len(errors)} pending_file_deletes-rad(er) er ikke trygge "
            "å behandle."
        )
        for pending_id, path, error in errors[:20]:
            doctor_info(
                f"pending_file_deletes #{pending_id}: path={path!r} ({error})"
            )
        doctor_report_omitted_details(len(errors))
        doctor_advice(
            "Ikke kjør cleanup-pending-deletes --apply før feilene er "
            "undersøkt mot databasen og sikkerhetskopien."
        )

    if ready:
        doctor_obs(
            f"{len(ready)} identifisert(e) ekstra fil(er) venter i "
            "pending_file_deletes."
        )
        doctor_pending_delete_details(ready)

    if missing:
        doctor_obs(
            f"{len(missing)} pending_file_deletes-rad(er) peker på filer "
            "som allerede mangler."
        )
        doctor_pending_delete_details(missing)

    if ready or missing:
        doctor_advice(
            "Doctor endrer ikke køen. Kontroller den med "
            "`bildebank cleanup-pending-deletes --list`."
        )


def doctor_pending_delete_details(
    rows: list[tuple[int, str, str, int, str | None]],
) -> None:
    for pending_id, path, reason, attempts, last_error in rows[:20]:
        doctor_info(
            f"pending_file_deletes #{pending_id}: {path} "
            f"(årsak={reason!r}, forsøk={attempts})"
        )
        if last_error is not None:
            doctor_info(f"  siste feil: {last_error}")
    doctor_report_omitted_details(len(rows))


def doctor_check_orphan_openclip_rows(target: Path) -> None:
    path = openclip_db_path(target)
    if not path.exists():
        doctor_ok("ingen OpenCLIP-database å kontrollere")
        return

    conn = connect_database_read_only(path)
    try:
        if not doctor_openclip_table_exists(conn, "image_embeddings"):
            doctor_obs("OpenCLIP-databasen mangler image_embeddings.")
            return
        main_db_uri = f"{db.db_path_for_target(target).resolve().as_uri()}?mode=ro"
        conn.execute("ATTACH DATABASE ? AS main_db", (main_db_uri,))
        embedding_rows = doctor_orphan_openclip_rows(conn, "image_embeddings")
        result_rows = (
            doctor_orphan_openclip_rows(conn, "image_search_results")
            if doctor_openclip_table_exists(conn, "image_search_results")
            else []
        )
    finally:
        conn.close()

    if not embedding_rows and not result_rows:
        doctor_ok("ingen foreldreløse OpenCLIP-rader")
        return

    if embedding_rows:
        total_embedding_rows = sum(int(row["row_count"]) for row in embedding_rows)
        doctor_error(
            f"{total_embedding_rows} OpenCLIP embedding-rad(er) peker på manglende eller slettet fil."
        )
        for row in embedding_rows[:20]:
            row_count = int(row["row_count"])
            count_suffix = f" ({row_count} rader)" if row_count > 1 else ""
            doctor_info(
                f"image_embeddings file #{int(row['file_id'])}: "
                f"{Path(str(row['target_path'])).as_posix()}{count_suffix}"
            )
        doctor_report_omitted_openclip_groups(len(embedding_rows))
    if result_rows:
        total_result_rows = sum(int(row["row_count"]) for row in result_rows)
        doctor_error(
            f"{total_result_rows} OpenCLIP søkeresultat-rad(er) peker på manglende eller slettet fil."
        )
        for row in result_rows[:20]:
            row_count = int(row["row_count"])
            count_suffix = f" ({row_count} rader)" if row_count > 1 else ""
            doctor_info(
                f"image_search_results file #{int(row['file_id'])}: "
                f"{Path(str(row['target_path'])).as_posix()}{count_suffix}"
            )
        doctor_report_omitted_openclip_groups(len(result_rows))
    doctor_advice("Kjør bildebank cleanup-image-search --apply")


def doctor_openclip_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def doctor_orphan_openclip_rows(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    if table not in {"image_embeddings", "image_search_results"}:
        raise ValueError(f"Uventet OpenCLIP-tabell: {table}")
    return list(
        conn.execute(
            f"""
            SELECT {table}.file_id, {table}.target_path, COUNT(*) AS row_count
            FROM {table}
            LEFT JOIN main_db.files ON main_db.files.id = {table}.file_id
            WHERE main_db.files.id IS NULL
               OR main_db.files.deleted_at IS NOT NULL
            GROUP BY {table}.file_id, {table}.target_path
            ORDER BY {table}.file_id, {table}.target_path
            """
        )
    )


def doctor_report_omitted_openclip_groups(total: int) -> None:
    if total > 20:
        doctor_info(f"... og {total - 20} file_id/sti-grupper til")


def doctor_check_files_on_disk(target: Path) -> None:
    conn = db.connect_read_only(target)
    try:
        rows = db.all_file_integrity_rows(conn)
    finally:
        conn.close()

    missing: list[tuple[int, str, Path]] = []
    not_regular: list[tuple[int, str, Path]] = []
    unreadable: list[tuple[int, str, Path, str]] = []
    wrong_size: list[tuple[int, str, Path, int, int]] = []
    progress = ProgressMeter("Doctor filer")
    progress.update(0, len(rows), action="kontrollert", force=True)
    for current, row in enumerate(rows, start=1):
        file_id = int(row["id"])
        state = "slettet" if row["deleted_at"] is not None else "aktiv"
        try:
            target_path = parse_collection_relative_path(row["target_path"])
        except InvalidCollectionRelativePath as exc:
            unreadable.append(
                (
                    file_id,
                    state,
                    Path(str(row["target_path"])),
                    f"databaseført filsti er ugyldig: {exc}",
                )
            )
        else:
            inspection = inspect_collection_file(
                target,
                target_path,
            )
            if inspection.status == COLLECTION_FILE_MISSING:
                missing.append((file_id, state, target_path))
            elif inspection.status == COLLECTION_FILE_NOT_REGULAR:
                not_regular.append((file_id, state, target_path))
            elif inspection.status != COLLECTION_FILE_OK:
                unreadable.append(
                    (
                        file_id,
                        state,
                        target_path,
                        inspection.message or "ukjent filsystemfeil",
                    )
                )
            else:
                expected_size = row["size_bytes"]
                if type(expected_size) is not int or expected_size < 0:
                    unreadable.append(
                        (
                            file_id,
                            state,
                            target_path,
                            f"ugyldig size_bytes i databasen: {expected_size!r}",
                        )
                    )
                else:
                    actual_size = inspection.size_bytes
                    if actual_size is None:
                        unreadable.append(
                            (
                                file_id,
                                state,
                                target_path,
                                "filstørrelsen kunne ikke leses",
                            )
                        )
                    elif actual_size != expected_size:
                        wrong_size.append(
                            (
                                file_id,
                                state,
                                target_path,
                                expected_size,
                                actual_size,
                            )
                        )
        progress.update(current, len(rows), action="kontrollert")
    progress.done(
        f"Doctor filer: ferdig kontrollert {len(rows)}/{len(rows)} filer."
    )

    active_count = sum(row["deleted_at"] is None for row in rows)
    deleted_count = len(rows) - active_count
    if not missing and not not_regular and not unreadable and not wrong_size:
        doctor_ok(
            f"alle {len(rows)} databasefiler finnes som vanlige filer med "
            "riktig størrelse "
            f"(aktive={active_count}, slettede={deleted_count})"
        )
        return

    if missing:
        doctor_error(
            f"{len(missing)} databasefil(er) mangler på disk."
        )
        for file_id, state, target_path in missing[:20]:
            doctor_info(
                f"file #{file_id} ({state}): {target_path.as_posix()}"
            )
        doctor_report_omitted_details(len(missing))

    if not_regular:
        doctor_error(
            f"{len(not_regular)} databasefil(er) er ikke vanlige filer uten "
            "lenker."
        )
        for file_id, state, target_path in not_regular[:20]:
            doctor_info(
                f"file #{file_id} ({state}): {target_path.as_posix()}"
            )
        doctor_report_omitted_details(len(not_regular))

    if unreadable:
        doctor_error(
            f"{len(unreadable)} databasefil(er) kunne ikke kontrolleres trygt."
        )
        for file_id, state, target_path, error in unreadable[:20]:
            doctor_info(
                f"file #{file_id} ({state}): "
                f"{target_path.as_posix()} ({error})"
            )
        doctor_report_omitted_details(len(unreadable))

    if wrong_size:
        doctor_error(
            f"{len(wrong_size)} databasefil(er) har feil størrelse."
        )
        for file_id, state, target_path, expected, actual in wrong_size[:20]:
            doctor_info(
                f"file #{file_id} ({state}): {target_path.as_posix()} "
                f"(database={expected}, disk={actual})"
            )
        doctor_report_omitted_details(len(wrong_size))

    doctor_advice(
        "Undersøk filene og sikkerhetskopien før du endrer databasen."
    )


def doctor_deep_check_active_file_hashes(target: Path) -> None:
    conn = db.connect_read_only(target)
    try:
        rows = db.active_file_integrity_rows(conn)
    finally:
        conn.close()

    missing: list[tuple[int, Path]] = []
    unreadable: list[tuple[int, Path, str]] = []
    wrong_hash: list[tuple[int, Path, str, str]] = []

    progress = ProgressMeter("Doctor SHA-256")
    progress.update(
        0,
        len(rows),
        action="kontrollert",
        eta=True,
        force=True,
    )
    for current, row in enumerate(rows, start=1):
        file_id = int(row["id"])
        try:
            target_path = parse_collection_relative_path(row["target_path"])
        except InvalidCollectionRelativePath:
            unreadable.append(
                (
                    file_id,
                    Path(str(row["target_path"])),
                    "databaseført filsti er ugyldig",
                )
            )
        else:
            inspection = inspect_collection_file(
                target,
                target_path,
            )
            if inspection.status == COLLECTION_FILE_MISSING:
                missing.append((file_id, target_path))
            elif inspection.status != COLLECTION_FILE_OK:
                unreadable.append(
                    (
                        file_id,
                        target_path,
                        inspection.message or "filen er ikke en vanlig fil",
                    )
                )
            else:
                try:
                    actual_sha256 = sha256_file(inspection.path)
                except OSError as exc:
                    unreadable.append((file_id, target_path, str(exc)))
                else:
                    expected_sha256 = str(row["sha256"])
                    if actual_sha256 != expected_sha256:
                        wrong_hash.append(
                            (
                                file_id,
                                target_path,
                                expected_sha256,
                                actual_sha256,
                            )
                        )
        progress.update(
            current,
            len(rows),
            action="kontrollert",
            eta=True,
        )
    progress.done(
        f"Doctor SHA-256: ferdig kontrollert {len(rows)}/{len(rows)} filer."
    )

    doctor_info(f"aktive databasefiler kontrollert: {len(rows)}")
    if not missing and not unreadable and not wrong_hash:
        doctor_ok(f"SHA-256 stemmer for alle {len(rows)} aktive filer")
        return

    if missing:
        doctor_error(f"{len(missing)} aktiv(e) fil(er) mangler på disk.")
        for file_id, target_path in missing[:20]:
            doctor_info(f"file #{file_id}: {target_path.as_posix()}")
        doctor_report_omitted_details(len(missing))

    if unreadable:
        doctor_error(f"{len(unreadable)} aktiv(e) fil(er) kunne ikke leses.")
        for file_id, target_path, error in unreadable[:20]:
            doctor_info(f"file #{file_id}: {target_path.as_posix()} ({error})")
        doctor_report_omitted_details(len(unreadable))

    if wrong_hash:
        doctor_error(f"{len(wrong_hash)} aktiv(e) fil(er) har feil SHA-256.")
        for file_id, target_path, expected, actual in wrong_hash[:20]:
            doctor_info(
                f"file #{file_id}: {target_path.as_posix()} "
                f"(database={expected}, disk={actual})"
            )
        doctor_report_omitted_details(len(wrong_hash))

    doctor_advice(
        "Undersøk filene og sikkerhetskopien før du endrer databasen."
    )


def doctor_check_orphan_files(target: Path) -> None:
    conn = db.connect_read_only(target)
    try:
        referenced_path_keys = db.file_target_path_keys(conn)
    finally:
        conn.close()

    orphan_files = doctor_find_orphan_files(target, referenced_path_keys)
    if not orphan_files:
        doctor_ok("ingen orphan-filer funnet i samlingen")
        return

    doctor_error(
        f"{len(orphan_files)} orphan-fil(er) finnes i samlingen uten databasepost."
    )
    for relative_path in orphan_files[:20]:
        doctor_info(f"orphan: {relative_path.as_posix()}")
    doctor_report_omitted_details(len(orphan_files))
    doctor_advice(
        "Undersøk filene og sikkerhetskopien før du endrer databasen."
    )


def doctor_find_orphan_files(
    target: Path,
    referenced_path_keys: set[str],
) -> list[Path]:
    managed_roots = sorted(
        (
            path
            for path in target.iterdir()
            if doctor_is_directory_without_links(path)
            and (
                path.name in {"udatert", "deleted"}
                or (len(path.name) == 4 and path.name.isdigit())
            )
        ),
        key=lambda path: path.name,
    )
    orphan_files: list[Path] = []
    scanned = 0
    progress = ProgressMeter("Doctor orphan")
    progress.update_count(0, action="scannet", force=True)
    for managed_root in managed_roots:
        for dirpath, dirnames, filenames in os.walk(
            managed_root,
            followlinks=False,
        ):
            dirnames[:] = [
                dirname
                for dirname in dirnames
                if doctor_is_directory_without_links(Path(dirpath) / dirname)
            ]
            for filename in filenames:
                path = Path(dirpath) / filename
                if not doctor_is_regular_file_without_links(path):
                    continue
                if not is_supported_media(path):
                    continue
                scanned += 1
                relative_path = path.relative_to(target)
                if db.relative_path_key(relative_path) not in referenced_path_keys:
                    orphan_files.append(relative_path)
                progress.update_count(scanned, action="scannet")
    progress.done(
        f"Doctor orphan: ferdig scannet {scanned} mediefiler."
    )
    return sorted(orphan_files, key=lambda path: path.as_posix())


def doctor_is_directory_without_links(path: Path) -> bool:
    try:
        path_stat = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(path_stat.st_mode) and not is_reparse_stat(path_stat)


def doctor_is_regular_file_without_links(path: Path) -> bool:
    try:
        path_stat = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(path_stat.st_mode) and not is_reparse_stat(path_stat)


def doctor_report_omitted_details(total: int) -> None:
    if total > 20:
        doctor_info(f"... og {total - 20} til")


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
