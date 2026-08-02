from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from . import db
from .config import OpenClipConfig
from .embedding_vectors import (
    ValidatedEmbeddingMatrix,
    load_validated_embedding_matrix,
)
from .media import IMAGE_EXTENSIONS
from .openclip import connect_openclip_db
from .server_browser_queries import source_item_ids
from .server_browser_sources import all_browser_source
from .server_filter import text_filter_browser_source
from .target_lock import TargetLock


DEFAULT_CLUSTER_COUNT = 20
DEFAULT_RANDOM_SEED = 0
DEFAULT_BATCH_SIZE = 1024
DEFAULT_N_INIT = 10
DEFAULT_MAX_ITER = 100
DEFAULT_REASSIGNMENT_RATIO = 0.01
CLUSTERING_ALGORITHM = "minibatch_kmeans"


@dataclass(frozen=True)
class ClusteringParameters:
    n_clusters: int
    random_seed: int = DEFAULT_RANDOM_SEED
    batch_size: int = DEFAULT_BATCH_SIZE
    n_init: int = DEFAULT_N_INIT
    max_iter: int = DEFAULT_MAX_ITER
    reassignment_ratio: float = DEFAULT_REASSIGNMENT_RATIO

    def __post_init__(self) -> None:
        if self.n_clusters < 1:
            raise ValueError("Antall grupper må være minst 1.")
        if self.batch_size < 1 or self.n_init < 1 or self.max_iter < 1:
            raise ValueError("Interne grupperingsparametere må være positive.")
        if not 0.0 <= self.reassignment_ratio <= 1.0:
            raise ValueError("reassignment_ratio må være mellom 0 og 1.")

    def as_dict(self) -> dict[str, int | float]:
        return {
            "n_clusters": self.n_clusters,
            "batch_size": self.batch_size,
            "random_state": self.random_seed,
            "n_init": self.n_init,
            "max_iter": self.max_iter,
            "reassignment_ratio": self.reassignment_ratio,
        }


@dataclass(frozen=True)
class ClusterMemberResult:
    file_id: int
    distance_to_center: float
    center_rank: int


@dataclass(frozen=True)
class ClusterResult:
    algorithm_label: int
    display_order: int
    center_embedding: bytes
    members: tuple[ClusterMemberResult, ...]


@dataclass(frozen=True)
class ClusteringResult:
    clusters: tuple[ClusterResult, ...]
    requested_cluster_count: int
    actual_cluster_count: int
    warning_message: str | None = None


@dataclass(frozen=True)
class ClusteringSelection:
    kind: str
    query: str | None
    hide_out_of_focus: bool

    @property
    def json_value(self) -> str:
        value: dict[str, object] = {
            "kind": self.kind,
            "hide_out_of_focus": self.hide_out_of_focus,
        }
        if self.query is not None:
            value["query"] = self.query
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class ClusteringRunResult:
    run_id: int
    status: str
    selected_file_count: int
    selected_image_count: int
    embedded_file_count: int
    missing_embedding_count: int
    invalid_embedding_count: int
    clustered_file_count: int
    actual_cluster_count: int
    embedding_dimension: int | None
    warning_message: str | None = None
    error_message: str | None = None


ClusteringProgress = Callable[[str, dict[str, object]], None]


def parse_clustering_selection(
    target: Path,
    query: str,
    *,
    hide_out_of_focus: bool = False,
) -> ClusteringSelection:
    clean_query = query.strip()
    if not clean_query:
        return ClusteringSelection("all", None, bool(hide_out_of_focus))
    source = text_filter_browser_source(clean_query, target)
    if bool(getattr(source.text_filter, "deleted", False)):
        raise ValueError("is:deleted kan ikke brukes som grupperingsutvalg.")
    return ClusteringSelection(
        "filter",
        str(source.text_filter.query),
        bool(hide_out_of_focus),
    )


def cluster_embedding_matrix(
    matrix: Any,
    file_ids: tuple[int, ...],
    parameters: ClusteringParameters,
) -> ClusteringResult:
    array = np.asarray(matrix, dtype=np.float32)
    if array.ndim != 2 or array.shape[0] != len(file_ids):
        raise ValueError(
            "Embeddingmatrisen og file_id-listen stemmer ikke overens."
        )
    if array.shape[0] < parameters.n_clusters:
        raise ValueError(
            f"Fant bare {array.shape[0]} gyldige embeddings, men antall "
            f"grupper er {parameters.n_clusters}."
        )
    if tuple(sorted(file_ids)) != file_ids or len(set(file_ids)) != len(file_ids):
        raise ValueError("file_id-er må være unike og sortert.")

    # Lazy import keeps sklearn and SciPy out of launcher and server processes.
    from sklearn.cluster import MiniBatchKMeans

    estimator = MiniBatchKMeans(
        n_clusters=parameters.n_clusters,
        batch_size=parameters.batch_size,
        random_state=parameters.random_seed,
        n_init=parameters.n_init,
        max_iter=parameters.max_iter,
        reassignment_ratio=parameters.reassignment_ratio,
    )
    labels = np.asarray(estimator.fit_predict(array), dtype=np.int64)
    centers = np.asarray(estimator.cluster_centers_, dtype=np.float32)
    grouped: list[
        tuple[int, bytes, tuple[ClusterMemberResult, ...]]
    ] = []
    for label in sorted({int(value) for value in labels.tolist()}):
        indexes = np.flatnonzero(labels == label)
        ranked = sorted(
            (
                (
                    float(np.linalg.norm(array[int(index)] - centers[label])),
                    int(file_ids[int(index)]),
                )
                for index in indexes
            ),
            key=lambda item: (item[0], item[1]),
        )
        members = tuple(
            ClusterMemberResult(file_id, distance, rank)
            for rank, (distance, file_id) in enumerate(ranked, start=1)
        )
        grouped.append((label, centers[label].tobytes(), members))
    grouped.sort(key=lambda item: (-len(item[2]), item[2][0].file_id))
    clusters = tuple(
        ClusterResult(label, display_order, center, members)
        for display_order, (label, center, members) in enumerate(
            grouped,
            start=1,
        )
    )
    actual_count = len(clusters)
    warning = None
    if actual_count < parameters.n_clusters:
        warning = (
            f"Kjøringen ga {actual_count} ikke-tomme grupper av "
            f"{parameters.n_clusters} ønskede."
        )
    return ClusteringResult(
        clusters,
        parameters.n_clusters,
        actual_count,
        warning,
    )


def run_image_clustering(
    target: Path,
    config: OpenClipConfig,
    *,
    query: str = "",
    hide_out_of_focus: bool = False,
    parameters: ClusteringParameters,
    progress: ClusteringProgress | None = None,
) -> ClusteringRunResult:
    selection = parse_clustering_selection(
        target,
        query,
        hide_out_of_focus=hide_out_of_focus,
    )
    with TargetLock(target, command="image-clustering"):
        openclip_conn = connect_openclip_db(target)
        run_id: int | None = None
        try:
            _recover_interrupted_runs(openclip_conn)
            run_id = _create_run(
                openclip_conn,
                selection,
                config,
                parameters,
            )
            _progress(progress, "run", run_id=run_id)
            selected_rows = _selected_file_rows(target, selection)
            selected_file_count = len(selected_rows)
            image_rows = tuple(
                (file_id, sha256)
                for file_id, sha256, stored_filename in selected_rows
                if Path(stored_filename).suffix.lower() in IMAGE_EXTENSIONS
            )
            _progress(
                progress,
                "selection",
                selected_file_count=selected_file_count,
                selected_image_count=len(image_rows),
            )
            embeddings = load_validated_embedding_matrix(
                openclip_conn,
                image_rows,
                model_name=config.model_name,
                pretrained=config.pretrained,
            )
            _update_run_counts(
                openclip_conn,
                run_id,
                selected_file_count=selected_file_count,
                selected_image_count=len(image_rows),
                embeddings=embeddings,
            )
            _progress(
                progress,
                "embeddings",
                model_name=config.model_name,
                pretrained=config.pretrained,
                dimension=embeddings.dimension,
                valid=embeddings.valid_count,
                missing=embeddings.missing_count,
                invalid=embeddings.invalid_count,
            )
            if embeddings.valid_count == 0:
                raise ValueError(
                    "Fant ingen gyldige embeddings for det valgte utvalget "
                    "og modellen."
                )
            _progress(
                progress,
                "algorithm",
                n_clusters=parameters.n_clusters,
                parameters=parameters.as_dict(),
            )
            clustering = cluster_embedding_matrix(
                embeddings.matrix,
                embeddings.file_ids,
                parameters,
            )
            _progress(
                progress,
                "storage",
                actual_cluster_count=clustering.actual_cluster_count,
            )
            _store_completed_run(
                openclip_conn,
                run_id,
                embeddings,
                clustering,
            )
            _progress(
                progress,
                "completed",
                run_id=run_id,
                actual_cluster_count=clustering.actual_cluster_count,
                largest_groups=[
                    len(cluster.members)
                    for cluster in clustering.clusters[:10]
                ],
            )
            return _run_result(openclip_conn, run_id)
        except KeyboardInterrupt:
            if run_id is not None:
                _finish_unsuccessful_run(
                    openclip_conn,
                    run_id,
                    "cancelled",
                    "Kjøringen ble avbrutt.",
                )
            raise
        except Exception as exc:
            if run_id is None:
                raise
            _progress(
                progress,
                "technical-error",
                error_type=type(exc).__name__,
                detail=str(exc),
            )
            error_message = _user_error_message(exc)
            _finish_unsuccessful_run(
                openclip_conn,
                run_id,
                "failed",
                error_message,
            )
            _progress(
                progress,
                "failed",
                run_id=run_id,
                error_message=error_message,
            )
            return _run_result(openclip_conn, run_id)
        finally:
            openclip_conn.close()


def delete_clustering_run(target: Path, run_id: int) -> bool:
    with TargetLock(target, command="delete-image-clustering-run"):
        conn = connect_openclip_db(target)
        try:
            cursor = conn.execute(
                "DELETE FROM image_clustering_runs WHERE id = ?",
                (run_id,),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def _selected_file_rows(
    target: Path,
    selection: ClusteringSelection,
) -> tuple[tuple[int, str, str], ...]:
    source = (
        all_browser_source()
        if selection.kind == "all"
        else text_filter_browser_source(selection.query or "", target)
    )
    ids = sorted(
        set(
            source_item_ids(
                target,
                source,
                hide_out_of_focus=selection.hide_out_of_focus,
            )
        )
    )
    if not ids:
        return ()
    conn = db.connect_read_only(target)
    try:
        rows: list[tuple[int, str, str]] = []
        for offset in range(0, len(ids), 900):
            chunk = ids[offset : offset + 900]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                f"""
                SELECT id, sha256, stored_filename
                FROM files
                WHERE deleted_at IS NULL AND id IN ({placeholders})
                ORDER BY id
                """,
                chunk,
            ):
                rows.append(
                    (
                        int(row["id"]),
                        str(row["sha256"]),
                        str(row["stored_filename"]),
                    )
                )
        return tuple(sorted(rows))
    finally:
        conn.close()


def _create_run(
    conn: sqlite3.Connection,
    selection: ClusteringSelection,
    config: OpenClipConfig,
    parameters: ClusteringParameters,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO image_clustering_runs(
            selection_kind, selection_json, model_name, pretrained,
            algorithm, parameters_json, random_seed, status, started_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, 'running', CURRENT_TIMESTAMP)
        """,
        (
            selection.kind,
            selection.json_value,
            config.model_name,
            config.pretrained,
            CLUSTERING_ALGORITHM,
            json.dumps(
                parameters.as_dict(),
                sort_keys=True,
                separators=(",", ":"),
            ),
            parameters.random_seed,
        ),
    )
    conn.commit()
    if cursor.lastrowid is None:
        raise ValueError("Databasen returnerte ikke run-ID.")
    return int(cursor.lastrowid)


def _recover_interrupted_runs(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE image_clustering_runs
        SET status = 'cancelled',
            error_message = 'Kjøringen ble avbrutt.',
            finished_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE status = 'running'
        """
    )
    conn.commit()


def _update_run_counts(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    selected_file_count: int,
    selected_image_count: int,
    embeddings: ValidatedEmbeddingMatrix,
) -> None:
    conn.execute(
        """
        UPDATE image_clustering_runs
        SET selected_file_count = ?,
            selected_image_count = ?,
            embedded_file_count = ?,
            missing_embedding_count = ?,
            invalid_embedding_count = ?,
            embedding_dimension = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            selected_file_count,
            selected_image_count,
            embeddings.valid_count,
            embeddings.missing_count,
            embeddings.invalid_count,
            embeddings.dimension,
            run_id,
        ),
    )
    conn.commit()


def _store_completed_run(
    conn: sqlite3.Connection,
    run_id: int,
    embeddings: ValidatedEmbeddingMatrix,
    result: ClusteringResult,
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        for cluster in result.clusters:
            cursor = conn.execute(
                """
                INSERT INTO image_clusters(
                    run_id, algorithm_label, display_order, kind,
                    center_embedding
                ) VALUES(?, ?, ?, 'cluster', ?)
                """,
                (
                    run_id,
                    cluster.algorithm_label,
                    cluster.display_order,
                    cluster.center_embedding,
                ),
            )
            if cursor.lastrowid is None:
                raise ValueError("Databasen returnerte ikke cluster-ID.")
            cluster_id = int(cursor.lastrowid)
            conn.executemany(
                """
                INSERT INTO image_cluster_members(
                    run_id, cluster_id, file_id, distance_to_center,
                    center_rank
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    (
                        run_id,
                        cluster_id,
                        member.file_id,
                        member.distance_to_center,
                        member.center_rank,
                    )
                    for member in cluster.members
                ),
            )
        conn.execute(
            """
            UPDATE image_clustering_runs
            SET status = 'completed',
                clustered_file_count = ?,
                actual_cluster_count = ?,
                warning_message = ?,
                finished_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                embeddings.valid_count,
                result.actual_cluster_count,
                result.warning_message,
                run_id,
            ),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _finish_unsuccessful_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    message: str,
) -> None:
    conn.rollback()
    conn.execute(
        "DELETE FROM image_clusters WHERE run_id = ?",
        (run_id,),
    )
    conn.execute(
        """
        UPDATE image_clustering_runs
        SET status = ?,
            error_message = ?,
            finished_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (status, message, run_id),
    )
    conn.commit()


def _run_result(
    conn: sqlite3.Connection,
    run_id: int,
) -> ClusteringRunResult:
    row = conn.execute(
        "SELECT * FROM image_clustering_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Fant ikke grupperingskjøring #{run_id}.")
    return ClusteringRunResult(
        run_id=run_id,
        status=str(row["status"]),
        selected_file_count=int(row["selected_file_count"]),
        selected_image_count=int(row["selected_image_count"]),
        embedded_file_count=int(row["embedded_file_count"]),
        missing_embedding_count=int(row["missing_embedding_count"]),
        invalid_embedding_count=int(row["invalid_embedding_count"]),
        clustered_file_count=int(row["clustered_file_count"]),
        actual_cluster_count=int(row["actual_cluster_count"]),
        embedding_dimension=(
            None
            if row["embedding_dimension"] is None
            else int(row["embedding_dimension"])
        ),
        warning_message=(
            None
            if row["warning_message"] is None
            else str(row["warning_message"])
        ),
        error_message=(
            None
            if row["error_message"] is None
            else str(row["error_message"])
        ),
    )


def _user_error_message(exc: Exception) -> str:
    if isinstance(exc, (ValueError, ImportError, ModuleNotFoundError)):
        return str(exc)[:500]
    return (
        "Grupperingen feilet. Se launcherloggen for tekniske detaljer."
    )


def _progress(
    progress: ClusteringProgress | None,
    stage: str,
    **values: object,
) -> None:
    if progress is not None:
        progress(stage, values)
