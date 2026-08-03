from __future__ import annotations

import html
import json
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from http import HTTPStatus
from pathlib import Path
from typing import Any

from . import db, server_request
from .image_clustering import delete_clustering_run
from .config import FaceRecognitionConfig
from .openclip import (
    connect_openclip_db_read_only,
    openclip_db_path,
)
from .server_browser_sources import (
    cluster_browser_source,
    parse_source_path,
)
from .server_endpoints_browser import respond_browser_source
from .server_pages import shell_page_html
from .sidecar_paths import regular_database_file_exists
from .target_lock import TargetLockError


@dataclass(frozen=True)
class GroupingClusterCard:
    id: int
    run_id: int
    display_order: int
    kind: str
    active_member_count: int
    date_from: str | None
    date_to: str | None
    unknown_date_count: int
    preview_file_ids: tuple[int, ...]
    tags: tuple[tuple[str, int], ...]
    people: tuple[tuple[str, int], ...]


def grouping_runs(target: Path) -> tuple[Any, ...]:
    if not regular_database_file_exists(openclip_db_path(target)):
        return ()
    conn = connect_openclip_db_read_only(target, full=False)
    try:
        return tuple(
            conn.execute(
                """
                SELECT *,
                    (SELECT COUNT(*) FROM image_clusters
                     WHERE image_clusters.run_id = image_clustering_runs.id)
                        AS cluster_row_count,
                    (SELECT COUNT(*) FROM image_cluster_members
                     WHERE image_cluster_members.run_id = image_clustering_runs.id)
                        AS member_row_count
                FROM image_clustering_runs
                ORDER BY id DESC
                """
            )
        )
    finally:
        conn.close()


def grouping_run(target: Path, run_id: int) -> Any | None:
    if not regular_database_file_exists(openclip_db_path(target)):
        return None
    conn = connect_openclip_db_read_only(target, full=False)
    try:
        return conn.execute(
            """
            SELECT *,
                (SELECT COUNT(*) FROM image_clusters
                 WHERE image_clusters.run_id = image_clustering_runs.id)
                    AS cluster_row_count,
                (SELECT COUNT(*) FROM image_cluster_members
                 WHERE image_cluster_members.run_id = image_clustering_runs.id)
                    AS member_row_count
            FROM image_clustering_runs
            WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
    finally:
        conn.close()


def grouping_cluster_display_order_and_kind(
    target: Path,
    run_id: int,
    cluster_id: int,
) -> tuple[int, str] | None:
    if not regular_database_file_exists(openclip_db_path(target)):
        return None
    fingerprint = _openclip_database_fingerprint(target)
    if fingerprint is None:
        return None
    return _cached_grouping_cluster_display_order_and_kind(
        str(target.resolve()),
        run_id,
        cluster_id,
        fingerprint,
    )


def _openclip_database_fingerprint(target: Path) -> tuple[int, int] | None:
    try:
        stat = openclip_db_path(target).stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


@lru_cache(maxsize=512)
def _cached_grouping_cluster_display_order_and_kind(
    target_path: str,
    run_id: int,
    cluster_id: int,
    fingerprint: tuple[int, int],
) -> tuple[int, str] | None:
    del fingerprint  # Used in the cache key to detect OpenCLIP database changes.
    target = Path(target_path)
    conn = connect_openclip_db_read_only(target, full=False)
    try:
        row = conn.execute(
            """
            SELECT display_order, kind
            FROM image_clusters
            WHERE id = ? AND run_id = ?
            """,
            (cluster_id, run_id),
        ).fetchone()
        return (
            None
            if row is None
            else (int(row["display_order"]), str(row["kind"]))
        )
    finally:
        conn.close()


def grouping_cluster_cards(
    target: Path,
    run_id: int,
    *,
    face_config: FaceRecognitionConfig | None = None,
) -> tuple[GroupingClusterCard, ...]:
    if not regular_database_file_exists(openclip_db_path(target)):
        return ()
    validation_conn = connect_openclip_db_read_only(target, full=False)
    validation_conn.close()
    conn = db.connect_read_only(target)
    try:
        uri = f"{openclip_db_path(target).resolve().as_uri()}?mode=ro"
        conn.execute("ATTACH DATABASE ? AS openclip_db", (uri,))
        summaries = tuple(
            conn.execute(
                """
                SELECT
                    image_clusters.id,
                    image_clusters.run_id,
                    image_clusters.display_order,
                    image_clusters.kind,
                    COUNT(files.id) AS active_member_count,
                    MIN(COALESCE(files.manual_date_from, files.taken_date))
                        AS date_from,
                    MAX(COALESCE(files.manual_date_to, files.taken_date))
                        AS date_to,
                    SUM(
                        CASE
                            WHEN files.id IS NOT NULL
                             AND files.manual_date_from IS NULL
                             AND files.taken_date IS NULL
                            THEN 1 ELSE 0
                        END
                    ) AS unknown_date_count
                FROM openclip_db.image_clusters AS image_clusters
                LEFT JOIN openclip_db.image_cluster_members AS members
                  ON members.cluster_id = image_clusters.id
                LEFT JOIN files
                  ON files.id = members.file_id
                 AND files.deleted_at IS NULL
                WHERE image_clusters.run_id = ?
                GROUP BY image_clusters.id
                ORDER BY image_clusters.display_order
                """,
                (run_id,),
            )
        )
        previews: dict[int, list[int]] = {}
        for preview_row in conn.execute(
            """
            WITH ranked_previews AS (
                SELECT
                    members.cluster_id,
                    members.file_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY members.cluster_id
                        ORDER BY members.center_rank, members.file_id
                    ) AS preview_rank
                FROM openclip_db.image_cluster_members AS members
                JOIN files ON files.id = members.file_id
                          AND files.deleted_at IS NULL
                WHERE members.run_id = ?
            )
            SELECT cluster_id, file_id
            FROM ranked_previews
            WHERE preview_rank <= 5
            ORDER BY cluster_id, preview_rank
            """,
            (run_id,),
        ):
            previews.setdefault(int(preview_row["cluster_id"]), []).append(
                int(preview_row["file_id"])
            )

        tags: dict[int, list[tuple[str, int, str]]] = {}
        for tag_row in conn.execute(
            """
            SELECT
                members.cluster_id,
                tags.name,
                tags.name_key,
                COUNT(DISTINCT files.id) AS file_count
            FROM openclip_db.image_cluster_members AS members
            JOIN files ON files.id = members.file_id
                      AND files.deleted_at IS NULL
            JOIN file_tags ON file_tags.file_id = files.id
            JOIN tags ON tags.id = file_tags.tag_id
            WHERE members.run_id = ?
            GROUP BY members.cluster_id, tags.id
            """,
            (run_id,),
        ):
            tags.setdefault(int(tag_row["cluster_id"]), []).append(
                (
                    str(tag_row["name"]),
                    int(tag_row["file_count"]),
                    str(tag_row["name_key"]),
                )
            )

        people = _grouping_people_by_cluster(
            conn,
            target,
            run_id,
            face_config=face_config,
        )
        cards: list[GroupingClusterCard] = []
        for summary in summaries:
            cluster_id = int(summary["id"])
            cluster_tags = sorted(
                tags.get(cluster_id, ()),
                key=lambda item: (-item[1], item[2]),
            )[:3]
            cards.append(
                GroupingClusterCard(
                    id=cluster_id,
                    run_id=int(summary["run_id"]),
                    display_order=int(summary["display_order"]),
                    kind=str(summary["kind"]),
                    active_member_count=int(summary["active_member_count"]),
                    date_from=(
                        None
                        if summary["date_from"] is None
                        else str(summary["date_from"])
                    ),
                    date_to=(
                        None
                        if summary["date_to"] is None
                        else str(summary["date_to"])
                    ),
                    unknown_date_count=int(summary["unknown_date_count"] or 0),
                    preview_file_ids=tuple(previews.get(cluster_id, ())),
                    tags=tuple(
                        (name, count)
                        for name, count, _name_key in cluster_tags
                    ),
                    people=people.get(cluster_id, ()),
                )
            )
        return tuple(cards)
    finally:
        conn.close()


def _grouping_people_by_cluster(
    conn: sqlite3.Connection,
    target: Path,
    run_id: int,
    *,
    face_config: FaceRecognitionConfig | None,
) -> dict[int, tuple[tuple[str, int], ...]]:
    from .face import face_db_path
    from .server_faces import current_face_db_path

    if not regular_database_file_exists(face_db_path(target, face_config)):
        return {}
    path = current_face_db_path(target, face_config)
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn.execute("ATTACH DATABASE ? AS face_db", (uri,))
    counts: dict[int, list[tuple[str, int]]] = {}
    for person_row in conn.execute(
        """
        WITH confirmed_files AS (
            SELECT members.cluster_id, person_faces.person_id, faces.file_id
            FROM openclip_db.image_cluster_members AS members
            JOIN files ON files.id = members.file_id
                      AND files.deleted_at IS NULL
            JOIN face_db.faces AS faces ON faces.file_id = members.file_id
            JOIN face_db.person_faces AS person_faces
              ON person_faces.face_id = faces.id
            WHERE members.run_id = ?
            UNION
            SELECT members.cluster_id, person_files.person_id,
                   person_files.file_id
            FROM openclip_db.image_cluster_members AS members
            JOIN files ON files.id = members.file_id
                      AND files.deleted_at IS NULL
            JOIN face_db.person_files AS person_files
              ON person_files.file_id = members.file_id
            WHERE members.run_id = ?
        )
        SELECT
            confirmed_files.cluster_id,
            persons.name,
            COUNT(*) AS file_count
        FROM confirmed_files
        JOIN face_db.persons AS persons
          ON persons.id = confirmed_files.person_id
        GROUP BY confirmed_files.cluster_id, confirmed_files.person_id
        """,
        (run_id, run_id),
    ):
        counts.setdefault(int(person_row["cluster_id"]), []).append(
            (str(person_row["name"]), int(person_row["file_count"]))
        )
    return {
        cluster_id: tuple(
            sorted(
                cluster_people,
                key=lambda item: (-item[1], item[0].casefold()),
            )[:3]
        )
        for cluster_id, cluster_people in counts.items()
    }


def grouping_page_html(server: Any) -> str:
    rows = grouping_runs(server.target)
    cards = "\n".join(_run_card_html(server, row) for row in rows)
    if not cards:
        cards = (
            "<p>Ingen grupperingskjøringer ennå. "
            "Start en kjøring fra Verktøy-fanen i launcheren.</p>"
        )
    return shell_page_html(
        "Gruppering",
        f"""
        <h1>Gruppering</h1>
        <p class="meta">
          En gruppering viser bilder som ligner på hverandre, samlet i grupper.
          Bildene blir ikke flyttet eller endret, og tagger og annen informasjon
          beholdes. Hvis du sletter en gruppering, fjernes bare denne inndelingen.
          Selve bildene blir liggende urørt i bildesamlingen.
          <a href="/help/web/gruppering.md">Les mer her.</a>
        </p>
        <div class="grouping-run-list">{cards}</div>
        """,
        face_enabled=server.face_enabled,
        openclip_enabled=server.openclip_enabled,
    )


def grouping_run_page_html(server: Any, run_id: int) -> str | None:
    row = grouping_run(server.target, run_id)
    if row is None:
        return None
    clusters = grouping_cluster_cards(
        server.target,
        run_id,
        face_config=server.config.face_recognition,
    )
    cluster_html = "\n".join(
        _cluster_card_html(cluster)
        for cluster in clusters
    )
    if not cluster_html:
        cluster_html = "<p>Ingen aktive grupper i denne kjøringen.</p>"
    selection_text = _run_selection_text(row)
    algorithm = str(row["algorithm"])
    algorithm_text = _run_algorithm_text(row)
    seed_html = (
        f'<div><dt>Seed</dt><dd>{int(row["random_seed"])}</dd></div>'
        if algorithm in {"minibatch_kmeans", "leiden"}
        else ""
    )
    graph_stats_html = _run_graph_stats_html(row)
    delete_form = _delete_run_form_html(
        server,
        row,
        label="Slett kjøring",
        button_class="danger-button",
    )
    error_html = (
        f'<p class="error">{html.escape(str(row["error_message"]))}</p>'
        if row["error_message"]
        else ""
    )
    warning_html = (
        f'<p class="message">{html.escape(str(row["warning_message"]))}</p>'
        if row["warning_message"]
        else ""
    )
    return shell_page_html(
        f"Gruppering #{run_id}",
        f"""
        <nav class="subnav"><a href="/grouping">Alle kjøringer</a></nav>
        <h1>Grupperingskjøring #{run_id}</h1>
        <dl class="info-list">
          <div><dt>Status</dt><dd>{html.escape(str(row["status"]))}</dd></div>
          <div><dt>Utvalg</dt><dd>{html.escape(selection_text)}</dd></div>
          <div><dt>Modell</dt><dd>{html.escape(str(row["model_name"]))} /
              {html.escape(str(row["pretrained"]))}</dd></div>
          <div><dt>Dimensjon</dt><dd>{row["embedding_dimension"] or "-"}</dd></div>
          <div><dt>Algoritme</dt><dd>{html.escape(algorithm_text)}</dd></div>
          {seed_html}
          <div><dt>Parametere</dt><dd><code>{html.escape(str(row["parameters_json"]))}</code></dd></div>
          <div><dt>Opprettet</dt><dd>{html.escape(str(row["created_at"]))}</dd></div>
          <div><dt>Startet</dt><dd>{html.escape(str(row["started_at"] or "-"))}</dd></div>
          <div><dt>Avsluttet</dt><dd>{html.escape(str(row["finished_at"] or "-"))}</dd></div>
          <div><dt>Valgt</dt><dd>{int(row["selected_file_count"])}</dd></div>
          <div><dt>Valgte stillbilder</dt><dd>{int(row["selected_image_count"])}</dd></div>
          <div><dt>Gyldige embeddings</dt><dd>{int(row["embedded_file_count"])}</dd></div>
          <div><dt>Gruppert</dt><dd>{int(row["clustered_file_count"])}</dd></div>
          <div><dt>Uten embedding</dt><dd>{int(row["missing_embedding_count"])}</dd></div>
          <div><dt>Ugyldig embedding</dt><dd>{int(row["invalid_embedding_count"])}</dd></div>
          {graph_stats_html}
        </dl>
        {warning_html}
        {error_html}
        <div class="grouping-cluster-list">{cluster_html}</div>
        {delete_form}
        """,
        face_enabled=server.face_enabled,
        openclip_enabled=server.openclip_enabled,
    )


def respond_grouping_cluster(handler: Any, raw_path: str) -> None:
    parts = raw_path.strip("/").split("/", 2)
    if len(parts) < 2:
        handler.respond_text("Ugyldig gruppeside.", status=HTTPStatus.NOT_FOUND)
        return
    try:
        run_id = int(parts[0])
        cluster_id = int(parts[1])
    except ValueError:
        handler.respond_text("Ugyldig gruppe-ID.", status=HTTPStatus.BAD_REQUEST)
        return
    cluster_identity = grouping_cluster_display_order_and_kind(
        handler.server.target,
        run_id,
        cluster_id,
    )
    if cluster_identity is None:
        handler.respond_text("Fant ikke gruppen.", status=HTTPStatus.NOT_FOUND)
        return
    display_order, kind = cluster_identity
    source = cluster_browser_source(
        run_id,
        cluster_id,
        display_order,
        kind=kind,
    )
    remainder = parts[2] if len(parts) == 3 else ""
    _source_part, page_mode, raw_value = parse_source_path(
        f"cluster/{remainder}" if remainder else "cluster"
    )
    respond_browser_source(
        handler,
        source,
        page_mode,
        raw_value,
        item_not_found_message="Filen finnes ikke lenger i denne gruppen.",
        invalid_page_message="Ugyldig gruppeside.",
    )


def respond_delete_grouping_run(handler: Any, run_id: int) -> None:
    row = grouping_run(handler.server.target, run_id)
    if row is None:
        handler.respond_text("Fant ikke kjøringen.", status=HTTPStatus.NOT_FOUND)
        return
    # Consume and validate form framing even though the path identifies the run.
    server_request.read_form_params(handler.headers, handler.rfile)
    try:
        deleted = delete_clustering_run(handler.server.target, run_id)
    except TargetLockError as exc:
        handler.respond_text(str(exc), status=HTTPStatus.CONFLICT)
        return
    if not deleted:
        handler.respond_text("Fant ikke kjøringen.", status=HTTPStatus.NOT_FOUND)
        return
    _cached_grouping_cluster_display_order_and_kind.cache_clear()
    handler.redirect("/grouping")


def _run_selection_text(row: Any) -> str:
    selection = json.loads(str(row["selection_json"]))
    return (
        "Alle aktive bilder"
        if str(row["selection_kind"]) == "all"
        else str(selection.get("query") or "")
    )


def _run_algorithm_text(row: Any) -> str:
    algorithm = str(row["algorithm"])
    if algorithm == "minibatch_kmeans":
        return "MiniBatchKMeans"
    if algorithm == "hdbscan":
        return "HDBSCAN"
    if algorithm == "leiden":
        return "Leiden"
    return algorithm


def _run_parameters_text(row: Any) -> str:
    raw_parameters = str(row["parameters_json"])
    try:
        parameters = json.loads(raw_parameters)
    except (TypeError, ValueError):
        return raw_parameters
    if not isinstance(parameters, dict):
        return raw_parameters

    algorithm = str(row["algorithm"])
    if algorithm == "minibatch_kmeans":
        group_count = parameters.get("n_clusters", "-")
        seed = parameters.get("random_state", row["random_seed"])
        return f"Ønskede grupper: {group_count} · Seed: {seed}"
    if algorithm == "hdbscan":
        minimum_size = parameters.get("min_cluster_size", "-")
        minimum_samples = parameters.get("min_samples")
        minimum_samples_text = (
            "samme som minste gruppestørrelse"
            if minimum_samples is None
            else str(minimum_samples)
        )
        return (
            f"Minste gruppestørrelse: {minimum_size} · "
            f"Min samples: {minimum_samples_text}"
        )
    if algorithm == "leiden":
        requested_k = parameters.get("requested_k", "-")
        mode = (
            "Gjensidige naboer"
            if parameters.get("neighbor_mode") == "mutual"
            else "Åpen graf"
        )
        resolution = parameters.get("resolution", "-")
        return (
            f"Naboer: {requested_k} · {mode} · "
            f"CPM-oppløsning: {resolution}"
        )
    return raw_parameters


def _run_graph_stats_html(row: Any) -> str:
    if str(row["algorithm"]) != "leiden":
        return ""

    def value(name: str) -> str:
        raw_value = row[name]
        return "Ikke beregnet" if raw_value is None else html.escape(str(raw_value))

    return f"""
      <div><dt>Effektive naboer</dt><dd>{value("effective_neighbor_count")}</dd></div>
      <div><dt>Grafnoder</dt><dd>{value("graph_node_count")}</dd></div>
      <div><dt>Grafkanter</dt><dd>{value("graph_edge_count")}</dd></div>
      <div><dt>Ugrupperte</dt><dd>{value("isolated_file_count")}</dd></div>
      <div><dt>Kanter fjernet av terskel</dt><dd>{value("threshold_removed_edge_count")}</dd></div>
      <div><dt>Median nærmeste likhet</dt><dd>{value("nearest_similarity_median")}</dd></div>
      <div><dt>Median likhet til nabo k</dt><dd>{value("kth_similarity_median")}</dd></div>
    """


def _delete_run_form_html(
    server: Any,
    row: Any,
    *,
    label: str,
    button_class: str,
) -> str:
    if getattr(server, "read_only", False):
        return ""
    run_id = int(row["id"])
    selection_text = _run_selection_text(row)
    confirmation = (
        f"Slette run #{run_id} med {int(row['cluster_row_count'])} "
        f"grupper og {int(row['member_row_count'])} medlemsrader? "
        f"Utvalg: {selection_text}"
    )
    confirmation_js = html.escape(json.dumps(confirmation), quote=True)
    return f"""
    <form method="post" action="/grouping/runs/{run_id}/delete"
          onsubmit="return confirm({confirmation_js})">
      <input type="hidden" name="csrf_token"
             value="{html.escape(str(server.csrf_token), quote=True)}">
      <button type="submit" class="{button_class}">{label}</button>
    </form>
    """


def _run_card_html(server: Any, row: Any) -> str:
    run_id = int(row["id"])
    selection_text = _run_selection_text(row)
    algorithm_text = _run_algorithm_text(row)
    parameters_text = _run_parameters_text(row)
    delete_form = _delete_run_form_html(
        server,
        row,
        label="Slett",
        button_class="inline-link danger-inline-link",
    )
    error_html = (
        f'<p class="error">{html.escape(str(row["error_message"]))}</p>'
        if row["error_message"]
        else ""
    )
    return f"""
    <article class="grouping-run-card">
      <h2><a href="/grouping/runs/{run_id}">Kjøring #{run_id}</a></h2>
      <p>{html.escape(str(row["status"]))} ·
         {html.escape(algorithm_text)} ·
         {int(row["actual_cluster_count"])} grupper ·
         {int(row["clustered_file_count"])} bilder</p>
      <p class="meta">Parametere: {html.escape(parameters_text)}</p>
      <p class="meta">Utvalg: {html.escape(selection_text)}</p>
      {error_html}
      {delete_form}
    </article>
    """


def _cluster_card_html(card: GroupingClusterCard) -> str:
    preview_html = "".join(
        f'<span class="grouping-preview">'
        f'<img src="/thumbnail/{file_id}" alt="" loading="lazy" '
        f'onerror="this.hidden=true;this.nextElementSibling.hidden=false">'
        f'<span class="grouping-preview-missing" hidden>Ingen miniatyr</span>'
        f'</span>'
        for file_id in card.preview_file_ids
    )
    tag_text = ", ".join(
        f"{name} ({count})" for name, count in card.tags
    )
    people_text = ", ".join(
        f"{name} ({count})" for name, count in card.people
    )
    date_text = (
        f"{card.date_from} – {card.date_to}"
        if card.date_from and card.date_to
        else "Ukjent dato"
    )
    if card.unknown_date_count:
        date_text += f" · {card.unknown_date_count} uten kjent dato"
    heading = (
        "Ugrupperte bilder"
        if card.kind == "noise"
        else f"Gruppe {card.display_order}"
    )
    return f"""
    <article class="grouping-cluster-card">
      <h2>{heading}</h2>
      <div class="grouping-previews">{preview_html}</div>
      <p>{card.active_member_count} aktive bilder</p>
      <p class="meta">{html.escape(date_text)}</p>
      <p class="meta">{html.escape(tag_text)}</p>
      <p class="meta">{html.escape(people_text)}</p>
      <a href="/grouping/runs/{card.run_id}/clusters/{card.id}">
        Vis alle bildene
      </a>
    </article>
    """
