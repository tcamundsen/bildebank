from __future__ import annotations

import html
import json
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


def grouping_runs(target: Path) -> tuple[Any, ...]:
    if not regular_database_file_exists(openclip_db_path(target)):
        return ()
    conn = connect_openclip_db_read_only(target)
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
    conn = connect_openclip_db_read_only(target)
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


def grouping_cluster_cards(
    target: Path,
    run_id: int,
) -> tuple[Any, ...]:
    if not regular_database_file_exists(openclip_db_path(target)):
        return ()
    validation_conn = connect_openclip_db_read_only(target)
    validation_conn.close()
    conn = db.connect_read_only(target)
    try:
        uri = f"{openclip_db_path(target).resolve().as_uri()}?mode=ro"
        conn.execute("ATTACH DATABASE ? AS openclip_db", (uri,))
        return tuple(
            conn.execute(
                """
                SELECT
                    image_clusters.id,
                    image_clusters.run_id,
                    image_clusters.display_order,
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
                    ) AS unknown_date_count,
                    (
                        SELECT members.file_id
                        FROM openclip_db.image_cluster_members AS members
                        JOIN files AS representative
                          ON representative.id = members.file_id
                         AND representative.deleted_at IS NULL
                        WHERE members.cluster_id = image_clusters.id
                        ORDER BY members.center_rank, members.file_id
                        LIMIT 1
                    ) AS representative_file_id
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
    finally:
        conn.close()


def grouping_cluster_preview_ids(
    target: Path,
    cluster_id: int,
    *,
    limit: int = 5,
) -> tuple[int, ...]:
    validation_conn = connect_openclip_db_read_only(target)
    validation_conn.close()
    conn = db.connect_read_only(target)
    try:
        uri = f"{openclip_db_path(target).resolve().as_uri()}?mode=ro"
        conn.execute("ATTACH DATABASE ? AS openclip_db", (uri,))
        return tuple(
            int(row["file_id"])
            for row in conn.execute(
                """
                SELECT members.file_id
                FROM openclip_db.image_cluster_members AS members
                JOIN files ON files.id = members.file_id
                          AND files.deleted_at IS NULL
                WHERE members.cluster_id = ?
                ORDER BY members.center_rank, members.file_id
                LIMIT ?
                """,
                (cluster_id, limit),
            )
        )
    finally:
        conn.close()


def grouping_cluster_tags(
    target: Path,
    cluster_id: int,
) -> tuple[tuple[str, int], ...]:
    validation_conn = connect_openclip_db_read_only(target)
    validation_conn.close()
    conn = db.connect_read_only(target)
    try:
        uri = f"{openclip_db_path(target).resolve().as_uri()}?mode=ro"
        conn.execute("ATTACH DATABASE ? AS openclip_db", (uri,))
        return tuple(
            (str(row["name"]), int(row["file_count"]))
            for row in conn.execute(
                """
                SELECT tags.name, COUNT(DISTINCT files.id) AS file_count
                FROM openclip_db.image_cluster_members AS members
                JOIN files ON files.id = members.file_id
                          AND files.deleted_at IS NULL
                JOIN file_tags ON file_tags.file_id = files.id
                JOIN tags ON tags.id = file_tags.tag_id
                WHERE members.cluster_id = ?
                GROUP BY tags.id
                ORDER BY file_count DESC, tags.name_key
                LIMIT 3
                """,
                (cluster_id,),
            )
        )
    finally:
        conn.close()


def grouping_page_html(server: Any) -> str:
    rows = grouping_runs(server.target)
    cards = "\n".join(_run_card_html(row) for row in rows)
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
          Kjøringene er reversible forslag. De endrer ikke bilder,
          tagger eller annen metadata.
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
    clusters = grouping_cluster_cards(server.target, run_id)
    cluster_html = "\n".join(
        _cluster_card_html(
            server.target,
            cluster,
            face_config=server.config.face_recognition,
        )
        for cluster in clusters
    )
    if not cluster_html:
        cluster_html = "<p>Ingen aktive grupper i denne kjøringen.</p>"
    selection = json.loads(str(row["selection_json"]))
    selection_text = (
        "Alle aktive bilder"
        if str(row["selection_kind"]) == "all"
        else str(selection.get("query") or "")
    )
    delete_form = ""
    if not getattr(server, "read_only", False):
        confirmation = (
            f"Slette run #{run_id} med {int(row['cluster_row_count'])} "
            f"grupper og {int(row['member_row_count'])} medlemsrader? "
            f"Utvalg: {selection_text}"
        )
        confirmation_js = html.escape(json.dumps(confirmation), quote=True)
        delete_form = f"""
        <form method="post" action="/grouping/runs/{run_id}/delete"
              onsubmit="return confirm({confirmation_js})">
          <input type="hidden" name="csrf_token"
                 value="{html.escape(str(server.csrf_token), quote=True)}">
          <button type="submit" class="danger-button">Slett kjøring</button>
        </form>
        """
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
          <div><dt>Seed</dt><dd>{int(row["random_seed"])}</dd></div>
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
    cards = grouping_cluster_cards(handler.server.target, run_id)
    cluster = next(
        (row for row in cards if int(row["id"]) == cluster_id),
        None,
    )
    if cluster is None:
        handler.respond_text("Fant ikke gruppen.", status=HTTPStatus.NOT_FOUND)
        return
    source = cluster_browser_source(
        run_id,
        cluster_id,
        int(cluster["display_order"]),
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
    handler.redirect("/grouping")


def _run_card_html(row: Any) -> str:
    run_id = int(row["id"])
    selection = json.loads(str(row["selection_json"]))
    selection_text = (
        "Alle aktive bilder"
        if str(row["selection_kind"]) == "all"
        else str(selection.get("query") or "")
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
         {int(row["actual_cluster_count"])} grupper ·
         {int(row["clustered_file_count"])} bilder</p>
      <p class="meta">{html.escape(selection_text)}</p>
      {error_html}
    </article>
    """


def grouping_cluster_people(
    target: Path,
    cluster_id: int,
    *,
    face_config: FaceRecognitionConfig | None = None,
) -> tuple[tuple[str, int], ...]:
    from .face import connect_face_db_read_only, face_db_path
    from .sidecar_paths import regular_database_file_exists

    path = face_db_path(target, face_config)
    if not regular_database_file_exists(path):
        return ()
    member_ids = grouping_cluster_preview_ids(
        target,
        cluster_id,
        limit=1000000,
    )
    if not member_ids:
        return ()
    conn = connect_face_db_read_only(target, face_config)
    try:
        counts: dict[str, set[int]] = {}
        for offset in range(0, len(member_ids), 900):
            chunk = member_ids[offset : offset + 900]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT persons.name, faces.file_id
                FROM persons
                JOIN person_faces ON person_faces.person_id = persons.id
                JOIN faces ON faces.id = person_faces.face_id
                WHERE faces.file_id IN ({placeholders})
                UNION
                SELECT persons.name, person_files.file_id
                FROM persons
                JOIN person_files ON person_files.person_id = persons.id
                WHERE person_files.file_id IN ({placeholders})
                """,
                (*chunk, *chunk),
            )
            for person_row in rows:
                counts.setdefault(
                    str(person_row["name"]),
                    set(),
                ).add(int(person_row["file_id"]))
        return tuple(
            sorted(
                (
                    (name, len(file_ids))
                    for name, file_ids in counts.items()
                ),
                key=lambda item: (-item[1], item[0].casefold()),
            )[:3]
        )
    finally:
        conn.close()


def _cluster_card_html(
    target: Path,
    row: Any,
    *,
    face_config: FaceRecognitionConfig | None = None,
) -> str:
    run_id = int(row["run_id"])
    cluster_id = int(row["id"])
    display_order = int(row["display_order"])
    previews = grouping_cluster_preview_ids(target, cluster_id)
    preview_html = "".join(
        f'<span class="grouping-preview">'
        f'<img src="/thumbnail/{file_id}" alt="" loading="lazy" '
        f'onerror="this.hidden=true;this.nextElementSibling.hidden=false">'
        f'<span class="grouping-preview-missing" hidden>Ingen miniatyr</span>'
        f'</span>'
        for file_id in previews
    )
    tags = grouping_cluster_tags(target, cluster_id)
    tag_text = ", ".join(f"{name} ({count})" for name, count in tags)
    people = grouping_cluster_people(
        target,
        cluster_id,
        face_config=face_config,
    )
    people_text = ", ".join(
        f"{name} ({count})" for name, count in people
    )
    date_from = row["date_from"]
    date_to = row["date_to"]
    date_text = (
        f"{date_from} – {date_to}"
        if date_from and date_to
        else "Ukjent dato"
    )
    unknown = int(row["unknown_date_count"] or 0)
    if unknown:
        date_text += f" · {unknown} uten kjent dato"
    return f"""
    <article class="grouping-cluster-card">
      <h2>Gruppe {display_order}</h2>
      <div class="grouping-previews">{preview_html}</div>
      <p>{int(row["active_member_count"])} aktive bilder</p>
      <p class="meta">{html.escape(date_text)}</p>
      <p class="meta">{html.escape(tag_text)}</p>
      <p class="meta">{html.escape(people_text)}</p>
      <a href="/grouping/runs/{run_id}/clusters/{cluster_id}">
        Vis alle bildene
      </a>
    </article>
    """
