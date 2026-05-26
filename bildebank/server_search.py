from __future__ import annotations

import html
import sqlite3
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .openclip import (
    ImageSearchResult,
    connect_openclip_db,
    create_search_run,
    load_text_model,
    relative_to_target,
    text_embedding,
)


DEFAULT_SEARCH_LIMIT = 100


@dataclass(frozen=True)
class ServerSearchStats:
    query: str
    results: tuple[ImageSearchResult, ...]


@dataclass(frozen=True)
class SearchEmbeddingCacheKey:
    model_name: str
    pretrained: str
    count: int
    updated_at: str | None


@dataclass(frozen=True)
class SearchEmbeddingRow:
    file_id: int
    target_path: Path
    target_path_key: str


@dataclass(frozen=True)
class SearchEmbeddingCache:
    key: SearchEmbeddingCacheKey
    matrix: Any
    rows: tuple[SearchEmbeddingRow, ...]


class OpenClipSearchCache:
    def __init__(self, config: Any) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._model: Any | None = None
        self._tokenizer: Any | None = None
        self._embeddings: SearchEmbeddingCache | None = None

    def text_vector(self, query: str) -> list[float]:
        with self._lock:
            if self._model is None or self._tokenizer is None:
                self._model, self._tokenizer = load_text_model(self.config.openclip)
            return text_embedding(self._model, self._tokenizer, query)

    def search(self, target: Path, query: str, limit: int) -> tuple[ImageSearchResult, ...]:
        with self._lock:
            if self._model is None or self._tokenizer is None:
                self._model, self._tokenizer = load_text_model(self.config.openclip)
            text_vector = normalized_search_vector(text_embedding(self._model, self._tokenizer, query))
            conn = connect_openclip_db(target)
            try:
                embeddings = self._cached_embeddings(conn)
                if embeddings.matrix.size == 0:
                    raise ValueError("Fant ingen bilde-embeddings. Kjør bildebank image-scan først.")
                scores = search_scores(embeddings.matrix, text_vector)
                top_indexes = top_score_indexes(scores, limit)
                run_id = create_search_run(conn, query, self.config.openclip, limit)
                results: list[ImageSearchResult] = []
                for rank, item_index in enumerate(top_indexes, start=1):
                    row = embeddings.rows[int(item_index)]
                    score = float(scores[int(item_index)])
                    conn.execute(
                        """
                        INSERT INTO image_search_results(run_id, file_id, target_path, target_path_key, similarity, rank)
                        VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            row.file_id,
                            row.target_path.as_posix(),
                            row.target_path_key,
                            score,
                            rank,
                        ),
                    )
                    results.append(ImageSearchResult(rank, row.file_id, row.target_path, score))
                conn.commit()
                return tuple(results)
            finally:
                conn.close()

    def _cached_embeddings(self, conn: sqlite3.Connection) -> SearchEmbeddingCache:
        key = search_embedding_cache_key(conn, self.config.openclip.model_name, self.config.openclip.pretrained)
        if key.count == 0:
            raise ValueError("Fant ingen bilde-embeddings. Kjør bildebank image-scan først.")
        if self._embeddings is None or self._embeddings.key != key:
            self._embeddings = load_search_embedding_cache(conn, key)
        return self._embeddings

    @property
    def loaded(self) -> bool:
        return self._model is not None and self._tokenizer is not None


def search_embedding_cache_key(conn: sqlite3.Connection, model_name: str, pretrained: str) -> SearchEmbeddingCacheKey:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count, MAX(updated_at) AS updated_at
        FROM image_embeddings
        WHERE model_name = ? AND pretrained = ?
        """,
        (model_name, pretrained),
    ).fetchone()
    return SearchEmbeddingCacheKey(
        model_name=model_name,
        pretrained=pretrained,
        count=int(row["count"]),
        updated_at=None if row["updated_at"] is None else str(row["updated_at"]),
    )


def load_search_embedding_cache(conn: sqlite3.Connection, key: SearchEmbeddingCacheKey) -> SearchEmbeddingCache:
    cursor = conn.execute(
        """
        SELECT file_id, target_path, target_path_key, embedding
        FROM image_embeddings
        WHERE model_name = ? AND pretrained = ?
        ORDER BY file_id
        """,
        (key.model_name, key.pretrained),
    )
    first = cursor.fetchone()
    if first is None:
        raise ValueError("Fant ingen bilde-embeddings. Kjør bildebank image-scan først.")
    first_vector = embedding_array_from_blob(bytes(first["embedding"]))
    matrix = np.zeros((key.count, first_vector.size), dtype=np.float32)
    rows: list[SearchEmbeddingRow] = []

    def add_row(index: int, row: sqlite3.Row) -> None:
        vector = embedding_array_from_blob(bytes(row["embedding"]))
        if vector.size == matrix.shape[1]:
            matrix[index, :] = normalized_search_vector(vector)
        rows.append(
            SearchEmbeddingRow(
                file_id=int(row["file_id"]),
                target_path=Path(str(row["target_path"])),
                target_path_key=str(row["target_path_key"]),
            )
        )

    add_row(0, first)
    index = 1
    for row in cursor:
        if index >= matrix.shape[0]:
            break
        add_row(index, row)
        index += 1
    if index < matrix.shape[0]:
        matrix = matrix[:index, :]
    return SearchEmbeddingCache(key, matrix, tuple(rows))


def embedding_array_from_blob(blob: bytes) -> Any:
    return np.frombuffer(blob, dtype=np.float32)


def normalized_search_vector(vector: Any) -> Any:
    array = np.asarray(vector, dtype=np.float32)
    norm = np.linalg.norm(array)
    if norm == 0.0:
        return array
    return array / norm


def search_scores(matrix: Any, text_vector: Any) -> Any:
    if matrix.shape[1] != text_vector.shape[0]:
        return np.zeros((matrix.shape[0],), dtype=np.float32)
    return matrix @ text_vector


def top_score_indexes(scores: Any, limit: int) -> list[int]:
    count = int(scores.shape[0])
    result_count = min(limit, count)
    if result_count <= 0:
        return []
    if result_count == count:
        candidates = np.arange(count)
    else:
        candidates = np.argpartition(scores, -result_count)[-result_count:]
    return sorted((int(index) for index in candidates), key=lambda index: (-float(scores[index]), index))


def search_server_images(server: Any, *, query: str, limit: int) -> ServerSearchStats:
    clean_query = query.strip()
    if not clean_query:
        raise ValueError("Søketekst kan ikke være tom.")
    results = server.search_cache.search(server.target, clean_query, limit)
    return ServerSearchStats(clean_query, results)


def search_form(query: str, limit: int = DEFAULT_SEARCH_LIMIT, *, model_loaded: bool = False) -> str:
    model_status = "true" if model_loaded else "false"
    return f"""
    <p class="search-loading" hidden data-search-loading>Laster bildesøkmodellen. Dette kan ta 10-20 sekunder...</p>
    <form action="/search" method="get" class="search" data-search-form data-model-loaded="{model_status}">
      <input name="q" value="{html.escape(query)}" placeholder="a photo of a beach" autofocus>
      <input name="limit" value="{limit}" inputmode="numeric" aria-label="Antall treff">
      <button type="submit">Søk</button>
    </form>
    """


def result_html(target: Path, result: ImageSearchResult) -> str:
    relative = relative_to_target(target, result.target_path)
    url = "/file/" + relative_path_url(relative)
    path_text = str(relative).replace("\\", "/")
    return f"""
    <article class="item">
      <a href="{html.escape(url)}"><img src="{html.escape(url)}" alt=""></a>
      <div class="text">
        <div class="path">#{result.rank} {html.escape(path_text)}</div>
        <div class="score">score={result.similarity:.3f}</div>
      </div>
    </article>
    """


def relative_path_url(path: Path) -> str:
    return urllib.parse.quote(str(path).replace("\\", "/"))
