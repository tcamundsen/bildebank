from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

from . import db
from .config import CONFIG_FILENAME, load_config
from .db_core import connect_database_read_only
from .exiftool import resolve_exiftool_path, validate_exiftool_install
from .face import face_db_path, face_db_summary, insightface_runtime_error
from .ffmpeg_tools import resolve_ffmpeg_tools
from .media import is_supported_media, sha256_file
from .openclip import openclip_db_path, openclip_db_summary, torch_gpu_status
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
        doctor_check_pending_file_moves(target)
        doctor_check_duplicate_active_sha256(target)
        doctor_check_active_files_have_sources(target)
        doctor_check_orphan_openclip_rows(target)
        doctor_check_active_files_exist(target)
        doctor_check_orphan_files(target)
        if deep:
            print()
            print("Dyp filintegritet:")
            doctor_deep_check_active_file_hashes(target)
    else:
        print()
        print("Aktiv bildesamling:")
        doctor_obs("ingen aktiv bildesamling funnet.")
        doctor_advice("Kjør kommandoen fra en bildesamling, eller bruk `--target`.")
        doctor_advice('Eksempel: "bildebank --target C:\\bildesamling doctor"')
    return 0


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


def doctor_check_active_files_have_sources(target: Path) -> None:
    conn = db.connect_read_only(target)
    try:
        rows = db.active_files_without_sources(conn)
    finally:
        conn.close()

    if not rows:
        doctor_ok("alle aktive files-rader har minst én file_sources-rad")
        return

    doctor_error(
        f"{len(rows)} aktiv(e) files-rad(er) mangler file_sources-proveniens."
    )
    for row in rows[:20]:
        doctor_info(
            f"file #{int(row['id'])}: {Path(str(row['target_path'])).as_posix()}"
        )
    doctor_report_omitted_details(len(rows))
    doctor_advice("Undersøk importfeilen og sikkerhetskopien før databasen endres.")


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


def doctor_check_active_files_exist(target: Path) -> None:
    conn = db.connect_read_only(target)
    try:
        rows = db.active_file_integrity_rows(conn)
    finally:
        conn.close()

    target_root = target.resolve()
    missing: list[tuple[int, Path]] = []
    invalid: list[tuple[int, Path]] = []
    progress = ProgressMeter("Doctor filer")
    progress.update(0, len(rows), action="kontrollert", force=True)
    for current, row in enumerate(rows, start=1):
        target_path = Path(str(row["target_path"]))
        resolved_path = db.absolute_target_path(target, target_path).resolve()
        try:
            resolved_path.relative_to(target_root)
        except ValueError:
            invalid.append((int(row["id"]), target_path))
        else:
            if not resolved_path.is_file():
                missing.append((int(row["id"]), target_path))
        progress.update(current, len(rows), action="kontrollert")
    progress.done(
        f"Doctor filer: ferdig kontrollert {len(rows)}/{len(rows)} filer."
    )

    if not missing and not invalid:
        doctor_ok(f"alle {len(rows)} aktive databasefiler finnes på disk")
        return

    if missing:
        doctor_error(
            f"{len(missing)} aktiv(e) databasefil(er) mangler på disk."
        )
        for file_id, target_path in missing[:20]:
            doctor_info(f"file #{file_id}: {target_path.as_posix()}")
        doctor_report_omitted_details(len(missing))

    if invalid:
        doctor_error(
            f"{len(invalid)} aktiv(e) databasefilsti(er) peker utenfor bildesamlingen."
        )
        for file_id, target_path in invalid[:20]:
            doctor_info(f"file #{file_id}: {target_path.as_posix()}")
        doctor_report_omitted_details(len(invalid))

    doctor_advice(
        "Undersøk filene og sikkerhetskopien før du endrer databasen."
    )


def doctor_deep_check_active_file_hashes(target: Path) -> None:
    conn = db.connect_read_only(target)
    try:
        rows = db.active_file_integrity_rows(conn)
    finally:
        conn.close()

    target_root = target.resolve()
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
        target_path = Path(str(row["target_path"]))
        resolved_path = db.absolute_target_path(target, target_path).resolve()
        try:
            resolved_path.relative_to(target_root)
        except ValueError:
            unreadable.append(
                (file_id, target_path, "filstien peker utenfor bildesamlingen")
            )
        else:
            if not resolved_path.is_file():
                missing.append((file_id, target_path))
            else:
                try:
                    actual_sha256 = sha256_file(resolved_path)
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
            if path.is_dir()
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
        for dirpath, _, filenames in os.walk(managed_root):
            for filename in filenames:
                path = Path(dirpath) / filename
                if not is_supported_media(path):
                    continue
                scanned += 1
                relative_path = db.target_relative_path(target, path)
                if db.relative_path_key(relative_path) not in referenced_path_keys:
                    orphan_files.append(relative_path)
                progress.update_count(scanned, action="scannet")
    progress.done(
        f"Doctor orphan: ferdig scannet {scanned} mediefiler."
    )
    return sorted(orphan_files, key=lambda path: path.as_posix())


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
