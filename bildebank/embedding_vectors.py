from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np


@dataclass(frozen=True)
class ValidatedEmbeddingMatrix:
    file_ids: tuple[int, ...]
    matrix: Any
    dimension: int | None
    valid_count: int
    missing_count: int
    invalid_count: int


def normalized_embedding_blob(
    blob: bytes,
    *,
    expected_dimension: int | None = None,
) -> Any:
    if not blob or len(blob) % np.dtype(np.float32).itemsize:
        raise ValueError("Embedding-BLOB har ugyldig lengde.")
    vector = np.frombuffer(blob, dtype=np.float32)
    if expected_dimension is not None and vector.size != expected_dimension:
        raise ValueError("Embedding har avvikende dimensjon.")
    if not np.isfinite(vector).all():
        raise ValueError("Embedding inneholder NaN eller uendelig verdi.")
    norm = float(np.linalg.norm(vector))
    if not np.isfinite(norm) or norm <= 0.0:
        raise ValueError("Embedding har ugyldig norm.")
    return np.asarray(vector / norm, dtype=np.float32)


def load_validated_embedding_matrix(
    conn: sqlite3.Connection,
    selected_files: Iterable[tuple[int, str]],
    *,
    model_name: str,
    pretrained: str,
) -> ValidatedEmbeddingMatrix:
    """Load exact model rows for sorted (file_id, sha256) identities."""
    selected = sorted(
        {(int(file_id), str(sha256)) for file_id, sha256 in selected_files}
    )
    rows_by_file: dict[int, sqlite3.Row] = {}
    if selected:
        selected_ids = [file_id for file_id, _sha256 in selected]
        for offset in range(0, len(selected_ids), 900):
            chunk = selected_ids[offset : offset + 900]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT file_id, sha256, model_name, pretrained, embedding
                FROM image_embeddings
                WHERE model_name = ? AND pretrained = ?
                  AND file_id IN ({placeholders})
                ORDER BY file_id
                """,
                (model_name, pretrained, *chunk),
            )
            for row in rows:
                rows_by_file[int(row["file_id"])] = row

    dimension: int | None = None
    file_ids: list[int] = []
    vectors: list[Any] = []
    missing_count = 0
    invalid_count = 0
    for file_id, sha256 in selected:
        row = rows_by_file.get(file_id)
        if row is None:
            missing_count += 1
            continue
        try:
            if (
                str(row["model_name"]) != model_name
                or str(row["pretrained"]) != pretrained
            ):
                raise ValueError("Embedding har feil modellnøkkel.")
            if str(row["sha256"]) != sha256:
                raise ValueError("Embedding har SHA-256-avvik.")
            vector = normalized_embedding_blob(
                bytes(row["embedding"]),
                expected_dimension=dimension,
            )
            if dimension is None:
                dimension = int(vector.size)
        except (TypeError, ValueError):
            invalid_count += 1
            continue
        file_ids.append(file_id)
        vectors.append(vector)

    matrix = (
        np.stack(vectors).astype(np.float32, copy=False)
        if vectors
        else np.empty((0, 0), dtype=np.float32)
    )
    return ValidatedEmbeddingMatrix(
        file_ids=tuple(file_ids),
        matrix=matrix,
        dimension=dimension,
        valid_count=len(file_ids),
        missing_count=missing_count,
        invalid_count=invalid_count,
    )
