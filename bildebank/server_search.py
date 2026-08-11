from __future__ import annotations

import html
import json
import re
import sqlite3
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from . import db
from .config import OpenClipConfig
from .embedding_vectors import normalized_embedding_blob
from .html_paths import relative_to_target
from .openclip import (
    ImageSearchResult,
    active_embedding_table,
    attach_main_database,
    connect_openclip_db,
    connect_openclip_db_read_only,
    create_search_run,
    load_text_model,
    text_embedding,
)
from .server_browser_sources import (
    BrowserSource,
    all_browser_source,
    search_results_browser_source,
    source_item_url,
)
from .target_lock import TargetLock


DEFAULT_SEARCH_LIMIT = 100
SIMILAR_SEARCH_QUERY_RE = re.compile(r"similar:file_id=([1-9]\d*)\Z")
SCOPED_SIMILAR_SEARCH_PREFIX = "similar:json="
ShellPageRenderer = Any


@dataclass(frozen=True)
class ServerSearchStats:
    query: str
    results: tuple[ImageSearchResult, ...]
    run_id: int | None = None


@dataclass(frozen=True)
class ServerSimilarSearchStats:
    reference_file_id: int
    reference_target_path: Path
    results: tuple[ImageSearchResult, ...]
    run_id: int | None = None
    scope_title: str | None = None
    scope_root_url: str | None = None


@dataclass(frozen=True)
class SimilarSearchQuery:
    reference_file_id: int
    scope_title: str | None = None
    scope_root_url: str | None = None


@dataclass(frozen=True)
class StoredVectorSearch:
    run_id: int
    results: tuple[ImageSearchResult, ...]


@dataclass(frozen=True)
class StoredSearchRun:
    run_id: int
    query: str
    model_name: str
    pretrained: str
    result_limit: int
    created_at: str
    results: tuple[ImageSearchResult, ...]

    @property
    def similar_reference_file_id(self) -> int | None:
        parsed = parse_similar_search_query(self.query)
        return parsed.reference_file_id if parsed is not None else None

    @property
    def similar_scope_title(self) -> str | None:
        parsed = parse_similar_search_query(self.query)
        return parsed.scope_title if parsed is not None else None

    @property
    def similar_scope_root_url(self) -> str | None:
        parsed = parse_similar_search_query(self.query)
        return parsed.scope_root_url if parsed is not None else None


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


def similar_search_query(
    reference_file_id: int,
    *,
    scope_title: str | None = None,
    scope_root_url: str | None = None,
) -> str:
    if scope_title is None or scope_root_url is None:
        return f"similar:file_id={reference_file_id}"
    payload = json.dumps(
        {
            "file_id": reference_file_id,
            "scope_root_url": scope_root_url,
            "scope_title": scope_title,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{SCOPED_SIMILAR_SEARCH_PREFIX}{payload}"


def parse_similar_search_query(query: str) -> SimilarSearchQuery | None:
    match = SIMILAR_SEARCH_QUERY_RE.fullmatch(query)
    if match is not None:
        return SimilarSearchQuery(int(match.group(1)))
    if not query.startswith(SCOPED_SIMILAR_SEARCH_PREFIX):
        return None
    try:
        payload = json.loads(query.removeprefix(SCOPED_SIMILAR_SEARCH_PREFIX))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    file_id = payload.get("file_id")
    scope_title = payload.get("scope_title")
    scope_root_url = payload.get("scope_root_url")
    if (
        not isinstance(file_id, int)
        or isinstance(file_id, bool)
        or file_id <= 0
        or not isinstance(scope_title, str)
        or not scope_title.strip()
        or not isinstance(scope_root_url, str)
        or not valid_local_source_root_url(scope_root_url)
    ):
        return None
    return SimilarSearchQuery(
        file_id,
        scope_title.strip(),
        scope_root_url,
    )


def valid_local_source_root_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return bool(
        parsed.path.startswith("/")
        and parsed.path == url
        and not parsed.scheme
        and not parsed.netloc
        and not parsed.query
        and not parsed.fragment
    )


class OpenClipSearchCache:
    def __init__(self, config: Any) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._model: Any | None = None
        self._tokenizer: Any | None = None
        self._embeddings: SearchEmbeddingCache | None = None
        self._preload_thread: threading.Thread | None = None

    def text_vector(self, query: str) -> list[float]:
        with self._lock:
            self._ensure_model_loaded()
            return text_embedding(self._model, self._tokenizer, query)

    def preload_model(self) -> None:
        with self._lock:
            self._ensure_model_loaded()

    def preload_model_async(self) -> str:
        if self.loaded:
            return "loaded"
        if self._preload_thread is not None and self._preload_thread.is_alive():
            return "loading"
        self._preload_thread = threading.Thread(target=self._preload_model_worker, daemon=True)
        self._preload_thread.start()
        return "loading"

    def _preload_model_worker(self) -> None:
        try:
            self.preload_model()
        except Exception:  # noqa: BLE001 - background preload should not crash the server
            pass

    def _ensure_model_loaded(self) -> None:
        if self._model is None or self._tokenizer is None:
            self._model, self._tokenizer = load_text_model(self.config.openclip)

    def search(
        self,
        target: Path,
        query: str,
        limit: int,
        *,
        hidden_file_ids: set[int] | None = None,
    ) -> StoredVectorSearch:
        with self._lock:
            self._ensure_model_loaded()
            text_vector = normalized_search_vector(text_embedding(self._model, self._tokenizer, query))
            conn = connect_openclip_db(target)
            try:
                attach_main_database(conn, target)
                embeddings = self._cached_embeddings(conn)
                if embeddings.matrix.size == 0:
                    raise ValueError("Fant ingen bilde-embeddings. Kjør bildebank image-scan først.")
                return self._store_vector_search(
                    conn,
                    embeddings,
                    query=query,
                    limit=limit,
                    search_vector=text_vector,
                    excluded_file_ids=hidden_file_ids,
                )
            finally:
                conn.close()

    def similar(
        self,
        target: Path,
        reference_file_id: int,
        limit: int = DEFAULT_SEARCH_LIMIT,
        *,
        hidden_file_ids: set[int] | None = None,
        candidate_file_ids: set[int] | None = None,
        scope_title: str | None = None,
        scope_root_url: str | None = None,
    ) -> StoredVectorSearch:
        limit = min(max(1, limit), DEFAULT_SEARCH_LIMIT)
        with self._lock:
            conn = connect_openclip_db(target)
            try:
                attach_main_database(conn, target)
                embeddings = self._cached_embeddings(conn)
                reference_index = next(
                    (
                        index
                        for index, row in enumerate(embeddings.rows)
                        if row.file_id == reference_file_id
                    ),
                    None,
                )
                if reference_index is None:
                    raise ValueError(
                        "Fant ingen embedding for referansebildet med valgt "
                        "OpenCLIP-modell. Kjør bildebank image-scan først."
                    )
                excluded_file_ids = set(hidden_file_ids or ())
                excluded_file_ids.add(reference_file_id)
                return self._store_vector_search(
                    conn,
                    embeddings,
                    query=similar_search_query(
                        reference_file_id,
                        scope_title=scope_title,
                        scope_root_url=scope_root_url,
                    ),
                    limit=limit,
                    search_vector=embeddings.matrix[reference_index, :],
                    excluded_file_ids=excluded_file_ids,
                    candidate_file_ids=candidate_file_ids,
                )
            finally:
                conn.close()

    def _store_vector_search(
        self,
        conn: sqlite3.Connection,
        embeddings: SearchEmbeddingCache,
        *,
        query: str,
        limit: int,
        search_vector: Any,
        excluded_file_ids: set[int] | None = None,
        candidate_file_ids: set[int] | None = None,
    ) -> StoredVectorSearch:
        scores = search_scores(embeddings.matrix, search_vector)
        excluded_file_ids = excluded_file_ids or set()
        if excluded_file_ids or candidate_file_ids is not None:
            eligible_indexes = [
                index
                for index, row in enumerate(embeddings.rows)
                if row.file_id not in excluded_file_ids
                and (
                    candidate_file_ids is None
                    or row.file_id in candidate_file_ids
                )
            ]
            eligible_scores = scores[
                np.asarray(eligible_indexes, dtype=np.int64)
            ]
            top_indexes = [
                eligible_indexes[index]
                for index in top_score_indexes(eligible_scores, limit)
            ]
        else:
            top_indexes = top_score_indexes(scores, limit)
        run_id = create_search_run(conn, query, self.config.openclip, limit)
        results: list[ImageSearchResult] = []
        for item_index in top_indexes:
            row = embeddings.rows[int(item_index)]
            score = float(scores[int(item_index)])
            rank = len(results) + 1
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
            if len(results) >= limit:
                break
        conn.commit()
        return StoredVectorSearch(run_id, tuple(results))

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
        f"""
        SELECT COUNT(*) AS count, MAX(updated_at) AS updated_at
        FROM {active_embedding_table(conn)}
        WHERE image_embeddings.model_name = ? AND image_embeddings.pretrained = ?
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
        f"""
        SELECT
            image_embeddings.file_id,
            image_embeddings.target_path,
            image_embeddings.target_path_key,
            image_embeddings.embedding
        FROM {active_embedding_table(conn)}
        WHERE image_embeddings.model_name = ? AND image_embeddings.pretrained = ?
        ORDER BY image_embeddings.file_id
        """,
        (key.model_name, key.pretrained),
    )
    expected_dimension: int | None = None
    vectors: list[Any] = []
    rows: list[SearchEmbeddingRow] = []

    def add_row(row: sqlite3.Row) -> None:
        nonlocal expected_dimension
        try:
            vector = normalized_embedding_blob(
                bytes(row["embedding"]),
                expected_dimension=expected_dimension,
            )
        except (TypeError, ValueError):
            return
        if expected_dimension is None:
            expected_dimension = int(vector.size)
        vectors.append(vector)
        rows.append(
            SearchEmbeddingRow(
                file_id=int(row["file_id"]),
                target_path=Path(str(row["target_path"])),
                target_path_key=str(row["target_path_key"]),
            )
        )
    for row in cursor:
        add_row(row)
    if not vectors:
        raise ValueError(
            "Fant ingen gyldige bilde-embeddings. "
            "Kjør bildebank image-scan først."
        )
    matrix = np.stack(vectors).astype(np.float32, copy=False)
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


def similar_search_source_from_url(server: Any, source_url: str) -> BrowserSource | None:
    source_url = source_url.strip()
    if not source_url or source_url == "/" or not valid_local_source_root_url(source_url):
        return None

    from .server_browser_queries import imported_source_by_id
    from .server_browser_sources import (
        cluster_browser_source,
        geo_place_browser_source,
        imported_source_browser_source,
        missing_face_suggestions_browser_source,
        parse_person_path,
        parse_person_reference_suggestions_path,
        parse_source_path,
        person_browser_source,
        person_reference_suggestions_browser_source,
        tag_browser_source,
    )

    source: BrowserSource | None = None
    if source_url.startswith("/filter/"):
        from .server_filter import text_filter_browser_source

        raw_query, page_mode, raw_value = parse_source_path(
            source_url.removeprefix("/filter/")
        )
        if page_mode is None and not raw_value:
            try:
                source = text_filter_browser_source(
                    urllib.parse.unquote(raw_query).strip(),
                    server.target,
                )
            except ValueError:
                source = None
    elif source_url.startswith("/source/"):
        raw_id = source_url.removeprefix("/source/")
        if raw_id.isdigit() and int(raw_id) > 0:
            source_row = imported_source_by_id(server.target, int(raw_id))
            if source_row is not None:
                source = imported_source_browser_source(source_row)
    elif source_url.startswith("/tag/"):
        raw_name, page_mode, raw_value = parse_source_path(
            source_url.removeprefix("/tag/")
        )
        if page_mode is None and not raw_value:
            tag_name = urllib.parse.unquote(raw_name).strip()
            try:
                conn = db.connect_read_only(server.target)
                try:
                    system_key = db.system_tag_key_for_name(conn, tag_name)
                finally:
                    conn.close()
                source = tag_browser_source(tag_name, system_key=system_key)
            except ValueError:
                source = None
    elif source_url.startswith("/geo/place/"):
        from .server_geo import geo_place_by_slug

        slug = urllib.parse.unquote(
            source_url.removeprefix("/geo/place/")
        ).strip()
        place = geo_place_by_slug(server.target, slug)
        if place is not None:
            source = geo_place_browser_source(place)
    elif source_url == "/people/missing-suggestions":
        source = missing_face_suggestions_browser_source()
    elif source_url.startswith("/people/") and "/references/" in source_url:
        from .server_faces import person_by_name

        raw_name, raw_file_id, page_mode, raw_value = (
            parse_person_reference_suggestions_path(
                source_url.removeprefix("/people/")
            )
        )
        person_name = urllib.parse.unquote(raw_name).strip()
        reference_file_id = urllib.parse.unquote(raw_file_id).strip()
        if (
            page_mode is None
            and not raw_value
            and reference_file_id.isdigit()
            and int(reference_file_id) > 0
        ):
            person = person_by_name(
                server.target,
                person_name,
                server.config.face_recognition,
            )
            if person is not None:
                source = person_reference_suggestions_browser_source(
                    str(person["name"]),
                    int(reference_file_id),
                )
    elif source_url.startswith("/person/"):
        from .server_faces import person_by_name

        raw_name, person_mode, show_faces, page_mode, raw_value = parse_person_path(
            source_url.removeprefix("/person/")
        )
        person_name = urllib.parse.unquote(raw_name).strip()
        if page_mode is None and not raw_value:
            person = person_by_name(
                server.target,
                person_name,
                server.config.face_recognition,
            )
            if person is not None:
                source = person_browser_source(
                    str(person["name"]),
                    include_suggestions=person_mode != "confirmed",
                    show_faces=show_faces,
                )
    elif source_url.startswith("/grouping/runs/"):
        from .server_endpoints_clustering import (
            grouping_cluster_display_order_and_kind,
        )

        match = re.fullmatch(
            r"/grouping/runs/([1-9]\d*)/clusters/([1-9]\d*)",
            source_url,
        )
        if match is not None:
            run_id, cluster_id = (int(value) for value in match.groups())
            identity = grouping_cluster_display_order_and_kind(
                server.target,
                run_id,
                cluster_id,
            )
            if identity is not None:
                display_order, kind = identity
                source = cluster_browser_source(
                    run_id,
                    cluster_id,
                    display_order,
                    kind=kind,
                )
    elif source_url.startswith("/search/runs/"):
        raw_id = source_url.removeprefix("/search/runs/")
        if raw_id.isdigit() and int(raw_id) > 0:
            run = load_stored_search_run(server.target, int(raw_id))
            if run is not None:
                source = stored_search_browser_source(server.target, run)

    return source if source is not None and source.root_url == source_url else None


def search_server_images(server: Any, *, query: str, limit: int) -> ServerSearchStats:
    clean_query = query.strip()
    if not clean_query:
        raise ValueError("Søketekst kan ikke være tom.")
    with TargetLock(server.target, command="image-search-web"):
        hidden_file_ids = None
        if server.config.browser.hide_out_of_focus:
            from .server_browser_queries import out_of_focus_file_ids

            hidden_file_ids = out_of_focus_file_ids(server.target)
        stored = server.search_cache.search(server.target, clean_query, limit, hidden_file_ids=hidden_file_ids)
        return ServerSearchStats(clean_query, stored.results, stored.run_id)


def search_server_similar_images(
    server: Any,
    *,
    file_id: int,
    limit: int = DEFAULT_SEARCH_LIMIT,
    source_url: str = "",
) -> ServerSimilarSearchStats:
    from .server_browser_queries import active_item_by_id_including_hidden, is_image_item

    limit = min(max(1, limit), DEFAULT_SEARCH_LIMIT)
    with TargetLock(server.target, command="image-similar-search-web"):
        reference_item = active_item_by_id_including_hidden(server.target, file_id)
        if reference_item is None or not is_image_item(reference_item):
            raise ValueError("Filen finnes ikke som et aktivt bilde.")
        scope = None
        candidate_file_ids = None
        if source_url.strip() and source_url.strip() != "/":
            scope = similar_search_source_from_url(server, source_url)
            if scope is None:
                raise ValueError("Utvalget for bildelikhetssøket finnes ikke lenger.")
            item_ids, item_positions = server.source_item_order(
                scope,
                hide_out_of_focus=server.config.browser.hide_out_of_focus,
            )
            if file_id not in item_positions:
                raise ValueError("Referansebildet finnes ikke i det valgte utvalget.")
            candidate_file_ids = set(item_ids)
        hidden_file_ids = None
        if server.config.browser.hide_out_of_focus:
            from .server_browser_queries import out_of_focus_file_ids

            hidden_file_ids = out_of_focus_file_ids(server.target)
        stored = server.search_cache.similar(
            server.target,
            file_id,
            limit,
            hidden_file_ids=hidden_file_ids,
            candidate_file_ids=candidate_file_ids,
            scope_title=scope.title if scope is not None else None,
            scope_root_url=scope.root_url if scope is not None else None,
        )
        return ServerSimilarSearchStats(
            reference_file_id=file_id,
            reference_target_path=Path(str(reference_item["target_path"])),
            results=stored.results,
            run_id=stored.run_id,
            scope_title=scope.title if scope is not None else None,
            scope_root_url=scope.root_url if scope is not None else None,
        )


def load_stored_search_run(target: Path, run_id: int) -> StoredSearchRun | None:
    if run_id <= 0:
        return None
    from .openclip import openclip_db_path

    if not openclip_db_path(target).is_file():
        return None
    conn = connect_openclip_db_read_only(target)
    try:
        row = conn.execute(
            """
            SELECT id, query, model_name, pretrained, result_limit, created_at
            FROM image_search_runs
            WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        results = tuple(
            ImageSearchResult(
                rank=int(result["rank"]),
                file_id=int(result["file_id"]),
                target_path=Path(str(result["target_path"])),
                similarity=float(result["similarity"]),
            )
            for result in conn.execute(
                """
                SELECT rank, file_id, target_path, similarity
                FROM image_search_results
                WHERE run_id = ?
                ORDER BY rank
                """,
                (run_id,),
            )
        )
        return StoredSearchRun(
            run_id=int(row["id"]),
            query=str(row["query"]),
            model_name=str(row["model_name"]),
            pretrained=str(row["pretrained"]),
            result_limit=int(row["result_limit"]),
            created_at=str(row["created_at"]),
            results=results,
        )
    finally:
        conn.close()


def stored_search_browser_source(
    target: Path,
    run: StoredSearchRun,
) -> BrowserSource:
    reference_file_id = run.similar_reference_file_id
    if reference_file_id is None:
        title = f"Bildesøk: {run.query}"
    else:
        from .server_browser_queries import active_item_by_id_including_hidden

        reference_item = active_item_by_id_including_hidden(target, reference_file_id)
        reference_name = (
            Path(str(reference_item["target_path"])).name
            if reference_item is not None
            else f"bilde-{reference_file_id}"
        )
        title = f"Lignende bilder: {reference_name}"
    return search_results_browser_source(run.run_id, title)


def search_start_html(
    openclip_config: OpenClipConfig,
    *,
    shell_page_html: ShellPageRenderer,
    query: str = "",
    limit: int = DEFAULT_SEARCH_LIMIT,
    model_loaded: bool = False,
    message: str = "",
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    return shell_page_html(
        "Bildesøk",
        f"""
        <h1>Bildesøk</h1>
        <p class="meta">OpenCLIP {html.escape(openclip_config.model_name)} ({html.escape(openclip_config.pretrained)})</p>
        {message_html(message)}
        {search_form(query, limit, model_loaded=model_loaded)}
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def search_html(
    target: Path,
    stats: ServerSearchStats,
    limit: int,
    *,
    shell_page_html: ShellPageRenderer,
    model_loaded: bool = False,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    source = (
        search_results_browser_source(stats.run_id, f"Bildesøk: {stats.query}")
        if stats.run_id is not None
        else None
    )
    return shell_page_html(
        f"Bildesøk: {stats.query}",
        f"""
        <h1>Bildesøk</h1>
        {search_form(stats.query, limit, model_loaded=model_loaded)}
        <p class="meta">{len(stats.results)} treff. Sortert med beste match først. Modell lastet: {'ja' if model_loaded else 'nei'}.</p>
        {search_results_grid_html(target, stats.results, source=source)}
        """,
        source=source,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def similar_search_html(
    target: Path,
    stats: ServerSimilarSearchStats,
    *,
    shell_page_html: ShellPageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    reference_name = stats.reference_target_path.name
    reference_url = source_item_url(all_browser_source(), stats.reference_file_id)
    scope_html = ""
    search_all_html = ""
    if stats.scope_title is not None and stats.scope_root_url is not None:
        reference_url = (
            f"{stats.scope_root_url.rstrip('/')}/item/{stats.reference_file_id}"
        )
        scope_html = (
            '<p class="meta">Søkt i utvalget '
            f'<a href="{html.escape(stats.scope_root_url)}">'
            f"«{html.escape(stats.scope_title)}»</a>.</p>"
        )
        search_all_html = f"""
        <form action="/search/similar" method="post" class="similar-search-form">
          <input type="hidden" name="file_id" value="{stats.reference_file_id}">
          <input type="hidden" name="limit" value="{DEFAULT_SEARCH_LIMIT}">
          <button type="submit">Søk i alle bilder</button>
        </form>
        """
    source = (
        search_results_browser_source(
            stats.run_id,
            f"Lignende bilder: {reference_name}",
        )
        if stats.run_id is not None
        else None
    )
    return shell_page_html(
        f"Bilder som ligner på {reference_name}",
        f"""
        <h1>Bilder som ligner på {html.escape(reference_name)}</h1>
        <p><a href="{html.escape(reference_url)}">Tilbake til referansebildet</a></p>
        {scope_html}
        {search_all_html}
        <p class="meta">{len(stats.results)} treff. Sortert med beste match først.</p>
        {search_results_grid_html(target, stats.results, source=source)}
        """,
        source=source,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def search_results_grid_html(
    target: Path,
    results: tuple[ImageSearchResult, ...],
    *,
    source: BrowserSource | None = None,
) -> str:
    items_by_id = search_result_items_by_id(target, results)
    items = "\n".join(
        result_html(
            target,
            result,
            items_by_id.get(result.file_id),
            source=source,
        )
        for result in results
    )
    return f"""
        <div class="grid">
          {items}
        </div>
    """


def message_html(message: str) -> str:
    if not message:
        return ""
    return f'<p class="message">{html.escape(message)}</p>'


def search_form(query: str, limit: int = DEFAULT_SEARCH_LIMIT, *, model_loaded: bool = False) -> str:
    model_status = "true" if model_loaded else "false"
    return f"""
    <p class="search-loading" hidden data-search-loading>Laster bildesøkmodellen. Dette kan ta 10-20 sekunder...</p>
    <form action="/search" method="post" class="search" data-search-form data-model-loaded="{model_status}">
      <input name="q" value="{html.escape(query)}" placeholder="a photo of a beach" autofocus>
      <input name="limit" value="{limit}" inputmode="numeric" aria-label="Antall treff">
      <button type="submit">Søk</button>
    </form>
    """


def search_result_items_by_id(target: Path, results: tuple[ImageSearchResult, ...]) -> dict[int, Any]:
    file_ids = [result.file_id for result in results]
    if not file_ids:
        return {}
    try:
        from .server_browser_queries import items_by_file_ids

        return {int(item["id"]): item for item in items_by_file_ids(target, file_ids)}
    except (OSError, sqlite3.Error, ValueError):
        return {}


def result_html(
    target: Path,
    result: ImageSearchResult,
    item: Any | None = None,
    *,
    source: BrowserSource | None = None,
) -> str:
    relative = relative_to_target(target, result.target_path)
    url = f"/file/{result.file_id}"
    item_url = source_item_url(source or all_browser_source(), result.file_id)
    path_text = str(relative).replace("\\", "/")
    link_class = ""
    rotation_style = ""
    if item is not None:
        from .server_browser_item_html import media_link_class_attr, rotation_style_attr

        link_class = media_link_class_attr(item)
        rotation_style = rotation_style_attr(item, target)
    return f"""
    <article class="item">
      <a href="{html.escape(item_url)}"{link_class}><img src="{html.escape(url)}" alt=""{rotation_style}></a>
      <div class="text">
        <div class="path">#{result.rank} {html.escape(path_text)}</div>
        <div class="score">score={result.similarity:.3f}</div>
      </div>
    </article>
    """
