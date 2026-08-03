from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import random
import sqlite3
import struct
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
DEFAULT_HDBSCAN_MIN_CLUSTER_SIZE = 5
DEFAULT_LEIDEN_NEIGHBOR_COUNT = 20
DEFAULT_LEIDEN_RESOLUTION = 0.2
DEFAULT_LEIDEN_ITERATIONS = -1
DEFAULT_LEIDEN_BETA = 0.01
LEIDEN_INPUT_FINGERPRINT_VERSION = 1
LEIDEN_KNN_CHUNK_SIZE = 256
MINIBATCH_KMEANS_ALGORITHM = "minibatch_kmeans"
HDBSCAN_ALGORITHM = "hdbscan"
LEIDEN_ALGORITHM = "leiden"


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

    @property
    def algorithm(self) -> str:
        return MINIBATCH_KMEANS_ALGORITHM


@dataclass(frozen=True)
class HdbscanParameters:
    min_cluster_size: int = DEFAULT_HDBSCAN_MIN_CLUSTER_SIZE
    min_samples: int | None = None
    metric: str = "euclidean"
    cluster_selection_method: str = "eom"
    cluster_selection_epsilon: float = 0.0
    alpha: float = 1.0
    leaf_size: int = 40
    allow_single_cluster: bool = False

    def __post_init__(self) -> None:
        if self.min_cluster_size < 2:
            raise ValueError("Minste gruppestørrelse må være minst 2.")
        if self.min_samples is not None and self.min_samples < 1:
            raise ValueError("Min samples må være minst 1.")
        if self.metric != "euclidean":
            raise ValueError("HDBSCAN støtter foreløpig bare euklidsk avstand.")
        if self.cluster_selection_method not in {"eom", "leaf"}:
            raise ValueError("Ugyldig cluster selection method.")
        if self.cluster_selection_epsilon < 0.0 or self.alpha <= 0.0:
            raise ValueError("Interne HDBSCAN-parametere er ugyldige.")
        if self.leaf_size < 1:
            raise ValueError("HDBSCAN leaf size må være positiv.")

    @property
    def algorithm(self) -> str:
        return HDBSCAN_ALGORITHM

    @property
    def random_seed(self) -> int:
        # HDBSCAN is deterministic and does not use a random seed. The run
        # schema keeps this field non-null for compatibility with older runs.
        return 0

    def as_dict(self) -> dict[str, int | float | str | bool | None]:
        return {
            "min_cluster_size": self.min_cluster_size,
            "min_samples": self.min_samples,
            "metric": self.metric,
            "cluster_selection_method": self.cluster_selection_method,
            "cluster_selection_epsilon": self.cluster_selection_epsilon,
            "alpha": self.alpha,
            "leaf_size": self.leaf_size,
            "allow_single_cluster": self.allow_single_cluster,
            "algorithm": "auto",
            "n_jobs": 1,
            "store_centers": "medoid",
            "copy": True,
        }


@dataclass(frozen=True)
class LeidenParameters:
    requested_k: int = DEFAULT_LEIDEN_NEIGHBOR_COUNT
    neighbor_mode: str = "union"
    minimum_similarity: float = 0.0
    weight_mode: str = "cosine"
    resolution: float = DEFAULT_LEIDEN_RESOLUTION
    random_seed: int = DEFAULT_RANDOM_SEED
    objective: str = "CPM"
    n_iterations: int = DEFAULT_LEIDEN_ITERATIONS
    beta: float = DEFAULT_LEIDEN_BETA

    def __post_init__(self) -> None:
        if not 1 <= self.requested_k <= 200:
            raise ValueError("Antall naboer må være mellom 1 og 200.")
        if self.neighbor_mode not in {"union", "mutual"}:
            raise ValueError("Nabomodus må være union eller mutual.")
        if not math.isfinite(self.minimum_similarity) or not (
            0.0 <= self.minimum_similarity <= 1.0
        ):
            raise ValueError("Minste likhet må være mellom 0 og 1.")
        if self.weight_mode not in {"unweighted", "cosine"}:
            raise ValueError("Kantvekter må være unweighted eller cosine.")
        if not math.isfinite(self.resolution) or self.resolution <= 0.0:
            raise ValueError("CPM-oppløsning må være større enn 0.")
        if self.objective != "CPM":
            raise ValueError("Leiden støtter foreløpig bare CPM.")
        if self.n_iterations == 0:
            raise ValueError("Antall Leiden-iterasjoner kan ikke være 0.")
        if not math.isfinite(self.beta) or self.beta <= 0.0:
            raise ValueError("Leiden beta må være større enn 0.")

    @property
    def algorithm(self) -> str:
        return LEIDEN_ALGORITHM

    def as_dict(self) -> dict[str, int | float | str]:
        return {
            "requested_k": self.requested_k,
            "neighbor_mode": self.neighbor_mode,
            "minimum_similarity": self.minimum_similarity,
            "weight_mode": self.weight_mode,
            "objective": self.objective,
            "resolution": self.resolution,
            "random_state": self.random_seed,
            "n_iterations": self.n_iterations,
            "beta": self.beta,
        }


ClusteringAlgorithmParameters = (
    ClusteringParameters | HdbscanParameters | LeidenParameters
)


@dataclass(frozen=True)
class ClusterMemberResult:
    file_id: int
    distance_to_center: float | None
    center_rank: int
    membership_score: float | None = None


@dataclass(frozen=True)
class ClusterResult:
    algorithm_label: int
    display_order: int
    center_embedding: bytes | None
    members: tuple[ClusterMemberResult, ...]
    kind: str = "cluster"


@dataclass(frozen=True)
class ClusteringResult:
    clusters: tuple[ClusterResult, ...]
    requested_cluster_count: int | None
    actual_cluster_count: int
    warning_message: str | None = None
    leiden_graph_stats: LeidenGraphStats | None = None


@dataclass(frozen=True)
class LeidenGraphStats:
    effective_neighbor_count: int
    node_count: int
    edge_count: int
    isolated_file_count: int
    threshold_removed_edge_count: int
    nearest_similarity_median: float
    kth_similarity_median: float


@dataclass(frozen=True)
class LeidenGraph:
    edges: tuple[tuple[int, int], ...]
    similarities: tuple[float, ...]
    isolated_indexes: tuple[int, ...]
    stats: LeidenGraphStats


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
    parameters: ClusteringAlgorithmParameters,
    *,
    progress: ClusteringProgress | None = None,
) -> ClusteringResult:
    array = np.asarray(matrix, dtype=np.float32)
    if array.ndim != 2 or array.shape[0] != len(file_ids):
        raise ValueError(
            "Embeddingmatrisen og file_id-listen stemmer ikke overens."
        )
    if tuple(sorted(file_ids)) != file_ids or len(set(file_ids)) != len(file_ids):
        raise ValueError("file_id-er må være unike og sortert.")
    if isinstance(parameters, LeidenParameters):
        return _cluster_embedding_matrix_leiden(
            array,
            file_ids,
            parameters,
            progress=progress,
        )
    if isinstance(parameters, HdbscanParameters):
        return _cluster_embedding_matrix_hdbscan(array, file_ids, parameters)
    return _cluster_embedding_matrix_minibatch_kmeans(array, file_ids, parameters)


def _cluster_embedding_matrix_minibatch_kmeans(
    array: Any,
    file_ids: tuple[int, ...],
    parameters: ClusteringParameters,
) -> ClusteringResult:
    if array.shape[0] < parameters.n_clusters:
        raise ValueError(
            f"Fant bare {array.shape[0]} gyldige embeddings, men antall "
            f"grupper er {parameters.n_clusters}."
        )

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


def _cluster_embedding_matrix_hdbscan(
    array: Any,
    file_ids: tuple[int, ...],
    parameters: HdbscanParameters,
) -> ClusteringResult:
    effective_min_samples = parameters.min_samples or parameters.min_cluster_size
    if array.shape[0] < effective_min_samples:
        raise ValueError(
            f"Fant bare {array.shape[0]} gyldige embeddings, men HDBSCAN "
            f"krever minst {effective_min_samples} med valgte parametere."
        )

    # Lazy import keeps sklearn and SciPy out of launcher and server processes.
    from sklearn.cluster import HDBSCAN

    estimator = HDBSCAN(
        min_cluster_size=parameters.min_cluster_size,
        min_samples=parameters.min_samples,
        metric=parameters.metric,
        cluster_selection_epsilon=parameters.cluster_selection_epsilon,
        alpha=parameters.alpha,
        algorithm="auto",
        leaf_size=parameters.leaf_size,
        n_jobs=1,
        cluster_selection_method=parameters.cluster_selection_method,
        allow_single_cluster=parameters.allow_single_cluster,
        store_centers="medoid",
        copy=True,
    )
    labels = np.asarray(estimator.fit_predict(array), dtype=np.int64)
    medoids = np.asarray(estimator.medoids_, dtype=np.float32)
    probabilities = np.asarray(estimator.probabilities_, dtype=np.float64)
    grouped: list[tuple[int, bytes, tuple[ClusterMemberResult, ...]]] = []
    for label in sorted({int(value) for value in labels.tolist() if int(value) >= 0}):
        indexes = np.flatnonzero(labels == label)
        ranked = sorted(
            (
                (
                    float(np.linalg.norm(array[int(index)] - medoids[label])),
                    int(file_ids[int(index)]),
                    float(probabilities[int(index)]),
                )
                for index in indexes
            ),
            key=lambda item: (item[0], item[1]),
        )
        members = tuple(
            ClusterMemberResult(file_id, distance, rank, membership_score)
            for rank, (distance, file_id, membership_score) in enumerate(
                ranked,
                start=1,
            )
        )
        grouped.append((label, medoids[label].tobytes(), members))
    grouped.sort(key=lambda item: (-len(item[2]), item[2][0].file_id))
    clusters: list[ClusterResult] = [
        ClusterResult(label, display_order, center, members)
        for display_order, (label, center, members) in enumerate(
            grouped,
            start=1,
        )
    ]
    noise_indexes = np.flatnonzero(labels == -1)
    if noise_indexes.size:
        noise_members = tuple(
            ClusterMemberResult(
                int(file_ids[int(index)]),
                None,
                rank,
                float(probabilities[int(index)]),
            )
            for rank, index in enumerate(
                sorted(noise_indexes.tolist(), key=lambda value: file_ids[int(value)]),
                start=1,
            )
        )
        clusters.append(
            ClusterResult(
                -1,
                len(clusters) + 1,
                None,
                noise_members,
                kind="noise",
            )
        )
    actual_count = len(grouped)
    warning = None
    if actual_count == 0:
        warning = "HDBSCAN fant ingen grupper; alle bildene ble ugrupperte."
    return ClusteringResult(
        tuple(clusters),
        None,
        actual_count,
        warning,
    )


def clustering_input_fingerprint(
    matrix: Any,
    file_ids: tuple[int, ...],
    *,
    model_name: str,
    pretrained: str,
) -> str:
    array = np.asarray(matrix, dtype="<f4", order="C")
    if array.ndim != 2 or array.shape[0] != len(file_ids):
        raise ValueError(
            "Embeddingmatrisen og file_id-listen stemmer ikke overens."
        )
    if tuple(sorted(file_ids)) != file_ids or len(set(file_ids)) != len(file_ids):
        raise ValueError("file_id-er må være unike og sortert.")

    digest = hashlib.sha256()
    digest.update(b"bildebank-leiden-input\x00")
    digest.update(struct.pack("<I", LEIDEN_INPUT_FINGERPRINT_VERSION))
    for value in (model_name, pretrained):
        encoded = value.encode("utf-8")
        digest.update(struct.pack("<I", len(encoded)))
        digest.update(encoded)
    digest.update(struct.pack("<QQ", array.shape[0], array.shape[1]))
    for index, file_id in enumerate(file_ids):
        digest.update(struct.pack("<q", file_id))
        digest.update(array[index].tobytes(order="C"))
    return digest.hexdigest()


def leiden_library_versions_json() -> str:
    versions = {
        package: importlib.metadata.version(package)
        for package in ("igraph", "numpy")
    }
    return json.dumps(versions, sort_keys=True, separators=(",", ":"))


def _cluster_embedding_matrix_leiden(
    array: Any,
    file_ids: tuple[int, ...],
    parameters: LeidenParameters,
    *,
    progress: ClusteringProgress | None,
) -> ClusteringResult:
    if array.shape[0] < 2:
        raise ValueError("Leiden-gruppering krever minst to gyldige bilder.")
    effective_k = min(parameters.requested_k, array.shape[0] - 1)
    _progress(
        progress,
        "neighbors",
        requested_k=parameters.requested_k,
        effective_k=effective_k,
    )
    neighbor_indexes, neighbor_similarities = _exact_cosine_neighbors(
        array,
        file_ids,
        effective_k,
    )
    _progress(progress, "graph", neighbor_mode=parameters.neighbor_mode)
    leiden_graph = _build_leiden_graph(
        neighbor_indexes,
        neighbor_similarities,
        parameters,
    )

    labels = np.full(array.shape[0], -1, dtype=np.int64)
    if leiden_graph.edges:
        _progress(
            progress,
            "leiden",
            nodes=array.shape[0] - len(leiden_graph.isolated_indexes),
            edges=len(leiden_graph.edges),
        )
        _assign_leiden_labels(labels, leiden_graph, parameters)

    _progress(progress, "ranking")
    grouped: list[tuple[int, bytes | None, tuple[ClusterMemberResult, ...]]] = []
    for label in sorted({int(value) for value in labels.tolist() if value >= 0}):
        indexes = np.flatnonzero(labels == label)
        center = np.asarray(array[indexes].mean(axis=0), dtype=np.float32)
        center_norm = float(np.linalg.norm(center))
        ranked_values: list[tuple[float | None, int]]
        if center_norm > 0.0 and math.isfinite(center_norm):
            center /= center_norm
            ranked_values = sorted(
                (
                    (
                        max(
                            0.0,
                            min(
                                2.0,
                                1.0
                                - float(
                                    np.dot(array[int(index)], center)
                                ),
                            ),
                        ),
                        int(file_ids[int(index)]),
                    )
                    for index in indexes
                ),
                key=lambda item: (item[0], item[1]),
            )
            center_blob: bytes | None = center.tobytes()
        else:
            ranked_values = [
                (None, int(file_ids[int(index)]))
                for index in sorted(
                    indexes.tolist(),
                    key=lambda value: file_ids[int(value)],
                )
            ]
            center_blob = None
        members = tuple(
            ClusterMemberResult(file_id, distance, rank)
            for rank, (distance, file_id) in enumerate(
                ranked_values,
                start=1,
            )
        )
        grouped.append((label, center_blob, members))
    grouped.sort(key=lambda item: (-len(item[2]), item[2][0].file_id))
    clusters: list[ClusterResult] = [
        ClusterResult(label, display_order, center, members)
        for display_order, (label, center, members) in enumerate(
            grouped,
            start=1,
        )
    ]

    if leiden_graph.isolated_indexes:
        noise_members = tuple(
            ClusterMemberResult(file_ids[index], None, rank)
            for rank, index in enumerate(
                sorted(
                    leiden_graph.isolated_indexes,
                    key=lambda value: file_ids[value],
                ),
                start=1,
            )
        )
        clusters.append(
            ClusterResult(
                -1,
                len(clusters) + 1,
                None,
                noise_members,
                kind="noise",
            )
        )

    warning_parts: list[str] = []
    if effective_k != parameters.requested_k:
        warning_parts.append(
            f"Antall naboer ble redusert fra {parameters.requested_k} "
            f"til {effective_k} fordi utvalget er lite."
        )
    if not grouped:
        warning_parts.append(
            "Leiden fant ingen grupper; alle bildene ble ugrupperte."
        )
    return ClusteringResult(
        tuple(clusters),
        None,
        len(grouped),
        " ".join(warning_parts) or None,
        leiden_graph.stats,
    )


def _exact_cosine_neighbors(
    array: Any,
    file_ids: tuple[int, ...],
    effective_k: int,
) -> tuple[Any, Any]:
    row_count = int(array.shape[0])
    neighbor_indexes = np.empty((row_count, effective_k), dtype=np.int64)
    neighbor_similarities = np.empty(
        (row_count, effective_k),
        dtype=np.float32,
    )
    for start in range(0, row_count, LEIDEN_KNN_CHUNK_SIZE):
        stop = min(start + LEIDEN_KNN_CHUNK_SIZE, row_count)
        similarities = np.asarray(
            array[start:stop] @ array.T,
            dtype=np.float32,
        )
        np.clip(similarities, -1.0, 1.0, out=similarities)
        for local_index, row in enumerate(similarities):
            matrix_index = start + local_index
            row[matrix_index] = -np.inf
            if effective_k == row_count - 1:
                candidates = np.flatnonzero(np.isfinite(row))
            else:
                boundary = float(np.partition(row, -effective_k)[-effective_k])
                candidates = np.flatnonzero(row >= boundary)
            ranked = sorted(
                (int(index) for index in candidates),
                key=lambda index: (-float(row[index]), file_ids[index]),
            )[:effective_k]
            neighbor_indexes[matrix_index] = ranked
            neighbor_similarities[matrix_index] = [
                row[index] for index in ranked
            ]
    return neighbor_indexes, neighbor_similarities


def _build_leiden_graph(
    neighbor_indexes: Any,
    neighbor_similarities: Any,
    parameters: LeidenParameters,
) -> LeidenGraph:
    node_count, effective_k = neighbor_indexes.shape
    directed: dict[tuple[int, int], float] = {}
    for source in range(node_count):
        for neighbor_rank in range(effective_k):
            target = int(neighbor_indexes[source, neighbor_rank])
            directed[(source, target)] = float(
                neighbor_similarities[source, neighbor_rank]
            )

    candidate_pairs = {
        (min(source, target), max(source, target))
        for source, target in directed
    }
    edges: list[tuple[int, int]] = []
    similarities: list[float] = []
    removed = 0
    degree = [0] * node_count
    for first, second in sorted(candidate_pairs):
        forward = directed.get((first, second))
        reverse = directed.get((second, first))
        if parameters.neighbor_mode == "mutual" and (
            forward is None or reverse is None
        ):
            continue
        similarity = max(
            value for value in (forward, reverse) if value is not None
        )
        if similarity <= 0.0 or similarity < parameters.minimum_similarity:
            removed += 1
            continue
        edges.append((first, second))
        similarities.append(similarity)
        degree[first] += 1
        degree[second] += 1

    isolated_indexes = tuple(
        index for index, value in enumerate(degree) if value == 0
    )
    nearest_median = float(np.median(neighbor_similarities[:, 0]))
    kth_median = float(np.median(neighbor_similarities[:, -1]))
    return LeidenGraph(
        tuple(edges),
        tuple(similarities),
        isolated_indexes,
        LeidenGraphStats(
            effective_neighbor_count=effective_k,
            node_count=node_count,
            edge_count=len(edges),
            isolated_file_count=len(isolated_indexes),
            threshold_removed_edge_count=removed,
            nearest_similarity_median=max(-1.0, min(1.0, nearest_median)),
            kth_similarity_median=max(-1.0, min(1.0, kth_median)),
        ),
    )


def _assign_leiden_labels(
    labels: Any,
    leiden_graph: LeidenGraph,
    parameters: LeidenParameters,
) -> None:
    # Lazy import keeps igraph out of launcher and server processes.
    import igraph

    isolated_indexes = set(leiden_graph.isolated_indexes)
    active_indexes = tuple(
        index
        for index in range(len(labels))
        if index not in isolated_indexes
    )
    dense_index = {
        original_index: index
        for index, original_index in enumerate(active_indexes)
    }
    graph = igraph.Graph(
        n=len(active_indexes),
        edges=[
            (dense_index[first], dense_index[second])
            for first, second in leiden_graph.edges
        ],
        directed=False,
    )
    weights = (
        None
        if parameters.weight_mode == "unweighted"
        else leiden_graph.similarities
    )
    igraph.set_random_number_generator(random.Random(parameters.random_seed))
    try:
        partition = graph.community_leiden(
            objective_function=parameters.objective,
            weights=weights,
            resolution=parameters.resolution,
            beta=parameters.beta,
            n_iterations=parameters.n_iterations,
        )
    finally:
        igraph.set_random_number_generator(None)
    for original_index, label in zip(
        active_indexes,
        partition.membership,
        strict=True,
    ):
        labels[original_index] = int(label)


def run_image_clustering(
    target: Path,
    config: OpenClipConfig,
    *,
    query: str = "",
    hide_out_of_focus: bool = False,
    parameters: ClusteringAlgorithmParameters,
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
            input_fingerprint: str | None = None
            library_versions_json: str | None = None
            if isinstance(parameters, LeidenParameters):
                input_fingerprint = clustering_input_fingerprint(
                    embeddings.matrix,
                    embeddings.file_ids,
                    model_name=config.model_name,
                    pretrained=config.pretrained,
                )
                library_versions_json = leiden_library_versions_json()
            _progress(
                progress,
                "algorithm",
                algorithm=parameters.algorithm,
                parameters=parameters.as_dict(),
            )
            clustering = cluster_embedding_matrix(
                embeddings.matrix,
                embeddings.file_ids,
                parameters,
                progress=progress,
            )
            _progress(
                progress,
                "storage",
                actual_cluster_count=clustering.actual_cluster_count,
            )
            _store_completed_run(
                openclip_conn,
                run_id,
                clustering,
                input_fingerprint=input_fingerprint,
                library_versions_json=library_versions_json,
            )
            normal_clusters = tuple(
                cluster
                for cluster in clustering.clusters
                if cluster.kind == "cluster"
            )
            noise_file_count = sum(
                len(cluster.members)
                for cluster in clustering.clusters
                if cluster.kind == "noise"
            )
            _progress(
                progress,
                "completed",
                run_id=run_id,
                actual_cluster_count=clustering.actual_cluster_count,
                ungrouped_file_count=noise_file_count,
                largest_groups=[
                    len(cluster.members)
                    for cluster in normal_clusters[:10]
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
    parameters: ClusteringAlgorithmParameters,
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
            parameters.algorithm,
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
    result: ClusteringResult,
    *,
    input_fingerprint: str | None = None,
    library_versions_json: str | None = None,
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        for cluster in result.clusters:
            cursor = conn.execute(
                """
                INSERT INTO image_clusters(
                    run_id, algorithm_label, display_order, kind,
                    center_embedding
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    cluster.algorithm_label,
                    cluster.display_order,
                    cluster.kind,
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
                    center_rank, membership_score
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        run_id,
                        cluster_id,
                        member.file_id,
                        member.distance_to_center,
                        member.center_rank,
                        member.membership_score,
                    )
                    for member in cluster.members
                ),
            )
        graph_stats = result.leiden_graph_stats
        conn.execute(
            """
            UPDATE image_clustering_runs
            SET status = 'completed',
                clustered_file_count = ?,
                actual_cluster_count = ?,
                warning_message = ?,
                input_fingerprint = ?,
                input_fingerprint_version = ?,
                effective_neighbor_count = ?,
                graph_node_count = ?,
                graph_edge_count = ?,
                isolated_file_count = ?,
                threshold_removed_edge_count = ?,
                nearest_similarity_median = ?,
                kth_similarity_median = ?,
                library_versions_json = ?,
                finished_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                sum(
                    len(cluster.members)
                    for cluster in result.clusters
                    if cluster.kind == "cluster"
                ),
                result.actual_cluster_count,
                result.warning_message,
                input_fingerprint,
                (
                    LEIDEN_INPUT_FINGERPRINT_VERSION
                    if input_fingerprint is not None
                    else None
                ),
                (
                    graph_stats.effective_neighbor_count
                    if graph_stats is not None
                    else None
                ),
                graph_stats.node_count if graph_stats is not None else None,
                graph_stats.edge_count if graph_stats is not None else None,
                (
                    graph_stats.isolated_file_count
                    if graph_stats is not None
                    else None
                ),
                (
                    graph_stats.threshold_removed_edge_count
                    if graph_stats is not None
                    else None
                ),
                (
                    graph_stats.nearest_similarity_median
                    if graph_stats is not None
                    else None
                ),
                (
                    graph_stats.kth_similarity_median
                    if graph_stats is not None
                    else None
                ),
                library_versions_json,
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
