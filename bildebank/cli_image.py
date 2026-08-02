from __future__ import annotations

import argparse
import webbrowser
from pathlib import Path

from .config import CONFIG_FILENAME, load_config
from .image_clustering import (
    ClusteringParameters,
    run_image_clustering,
)
from .openclip import (
    cleanup_image_search,
    openclip_db_path,
    repair_image_search_paths,
    scan_images,
    search_images,
)
from .progress import ProgressMeter
from .target_lock import TargetLock


IMAGE_SCAN_PROGRESS: ProgressMeter | None = None
IMAGE_SEARCH_PROGRESS: ProgressMeter | None = None


def run_image_command(args: argparse.Namespace, target: Path, *, repo_root: Path) -> int:
    if args.command == "cleanup-image-search":
        return run_cleanup_image_search(target, apply=args.apply)
    if args.command == "repair-image-search-paths":
        return run_repair_image_search_paths(target, apply=args.apply)
    require_openclip_enabled(load_config(repo_root).openclip.enabled)
    if args.command == "_image-clustering-worker":
        return run_image_clustering_worker(
            target,
            repo_root=repo_root,
            query=args.filter,
            hide_out_of_focus=args.hide_out_of_focus,
            n_clusters=args.clusters,
            random_seed=args.seed,
        )
    if args.command == "image-scan":
        return run_image_scan(target, repo_root=repo_root, limit=args.limit)
    return run_image_search(
        target,
        repo_root=repo_root,
        query=args.query,
        limit=args.limit,
        browser=not args.no_browser,
    )


def run_image_clustering_worker(
    target: Path,
    *,
    repo_root: Path,
    query: str,
    hide_out_of_focus: bool,
    n_clusters: int,
    random_seed: int,
) -> int:
    config = load_config(repo_root).openclip
    parameters = ClusteringParameters(
        n_clusters=n_clusters,
        random_seed=random_seed,
    )
    print(
        "Image-clustering: starter "
        f"clusters={n_clusters}, seed={random_seed}, "
        f"batch_size={parameters.batch_size}, n_init={parameters.n_init}, "
        f"max_iter={parameters.max_iter}, "
        f"reassignment_ratio={parameters.reassignment_ratio}"
    )
    result = run_image_clustering(
        target,
        config,
        query=query,
        hide_out_of_focus=hide_out_of_focus,
        parameters=parameters,
        progress=print_image_clustering_progress,
    )
    print(
        "Image-clustering: "
        f"run_id={result.run_id}, status={result.status}, "
        f"valgt={result.selected_file_count}, bilder={result.selected_image_count}, "
        f"gyldige={result.embedded_file_count}, "
        f"mangler={result.missing_embedding_count}, "
        f"ugyldige={result.invalid_embedding_count}, "
        f"gruppert={result.clustered_file_count}, "
        f"grupper={result.actual_cluster_count}"
    )
    if result.warning_message:
        print(f"Image-clustering: advarsel={result.warning_message}")
    if result.status != "completed":
        print(f"Image-clustering: feil={result.error_message}")
        return 2
    print(
        "Image-clustering: ferdig. Resultatet finnes under Gruppering "
        "i webgrensesnittet."
    )
    return 0


def print_image_clustering_progress(
    stage: str,
    values: dict[str, object],
) -> None:
    details = ", ".join(
        f"{key}={value}"
        for key, value in values.items()
    )
    if stage == "algorithm":
        print("Image-clustering: Grupperer bilder ...")
    elif details:
        print(f"Image-clustering: {stage}: {details}")
    else:
        print(f"Image-clustering: {stage}")


def run_image_scan(target: Path, *, repo_root: Path, limit: int | None) -> int:
    config = load_config(repo_root).openclip
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


def run_image_search(
    target: Path,
    *,
    repo_root: Path,
    query: str,
    limit: int,
    browser: bool = True,
) -> int:
    config = load_config(repo_root).openclip
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


def run_cleanup_image_search(target: Path, *, apply: bool) -> int:
    with TargetLock(target, command="cleanup-image-search"):
        stats = cleanup_image_search(target, apply=apply)
    if not stats.exists:
        print("Ingen OpenCLIP-database å rydde.")
        return 0
    print(
        "Bildesøk-opprydding: "
        f"foreldreløse_embeddings={stats.embedding_rows}, "
        f"foreldreløse_søkeresultater={stats.search_result_rows}, "
        f"foreldreløse_cluster-medlemmer={stats.cluster_member_rows}"
    )
    for group in stats.groups[:20]:
        suffix = f" ({group.row_count} rader)" if group.row_count > 1 else ""
        print(
            f"{group.table}\tfile #{group.file_id}\t"
            f"{group.target_path.as_posix()}{suffix}"
        )
    if len(stats.groups) > 20:
        print(f"... og {len(stats.groups) - 20} file_id/sti-grupper til")
    if not apply:
        print("Dry-run: ingen endringer er gjort.")
        if (
            stats.embedding_rows
            or stats.search_result_rows
            or stats.cluster_member_rows
        ):
            print("Kjør: bildebank cleanup-image-search --apply")
        return 0
    print(
        "Slettet: "
        f"image_embeddings={stats.deleted_embedding_rows}, "
        f"image_search_results={stats.deleted_search_result_rows}, "
        f"image_cluster_members={stats.deleted_cluster_member_rows}, "
        f"tomme_image_clusters={stats.deleted_empty_clusters}, "
        f"tomme_image_search_runs={stats.deleted_search_runs}"
    )
    return 0


def run_repair_image_search_paths(target: Path, *, apply: bool) -> int:
    with TargetLock(target, command="repair-image-search-paths"):
        stats = repair_image_search_paths(target, apply=apply)
    if not stats.exists:
        print("Ingen OpenCLIP-database å reparere.")
        return 0
    print(
        "Bildesøk-stireparasjon: "
        f"reparerbare_embeddingrader={stats.repairable_rows}, "
        f"SHA-avvik_som_ikke_røres={stats.unrepairable_sha_rows}"
    )
    for group in stats.groups[:20]:
        suffix = f" ({group.row_count} rader)" if group.row_count > 1 else ""
        print(
            f"file #{group.file_id}\t"
            f"{group.stored_target_path.as_posix()} -> "
            f"{group.expected_target_path.as_posix()}{suffix}"
        )
    if len(stats.groups) > 20:
        print(f"... og {len(stats.groups) - 20} file_id/sti-grupper til")
    if not apply:
        print("Dry-run: ingen endringer er gjort.")
        if stats.repairable_rows:
            print("Ta et oppdatert snapshot før du bruker --apply.")
            print("Kjør: bildebank repair-image-search-paths --apply")
    else:
        print(f"Oppdatert: image_embeddings={stats.updated_rows}")
    if stats.unrepairable_sha_rows:
        print(
            "SHA-256-avvik ble ikke reparert. Undersøk hoveddatabasen, "
            "mediefilene og sikkerhetskopien."
        )
        return 2
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


def require_openclip_enabled(enabled: bool) -> None:
    if not enabled:
        raise ValueError(
            f"Tekstbasert bildesøk er av. Kjør `bildebank config image_search enable` "
            f"eller sett enabled = true under [image_search] i {CONFIG_FILENAME} hvis du vil teste."
        )


def open_file_in_browser(path: Path) -> None:
    if not webbrowser.open(path.resolve().as_uri()):
        raise ValueError(f"Klarte ikke åpne nettleseren for: {path}")
