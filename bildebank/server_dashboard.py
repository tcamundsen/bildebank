from __future__ import annotations

import datetime as dt
import html
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, cast

from . import db
from .config import AppConfig
from .formatting import format_bytes
from .media import VIDEO_EXTENSIONS
from .program_state import KnownSnapshotRepository, known_snapshot_repositories


ShellPageRenderer = Callable[..., str]


@dataclass(frozen=True)
class DashboardAction:
    title: str
    severity: str
    detail: str
    command: str | None = None
    help_path: str | None = None
    link_path: str | None = None
    maintenance_name: str | None = None
    gui_label: str | None = None
    thumbnail_maintenance: bool = False
    fact_rows: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class DashboardSummary:
    total_active: int
    total_active_size_bytes: int
    active_images: int
    active_image_size_bytes: int
    active_videos: int
    active_video_size_bytes: int
    deleted_files: int
    deleted_file_size_bytes: int
    source_status_counts: dict[str, int]
    source_file_count: int
    duplicate_source_count: int
    unresolved_errors: int
    name_conflicts: int
    undated_files: int
    pending_file_moves: int
    date_source_counts: dict[str, int]
    geo_stats: dict[str, int]
    snapshot_repositories: tuple[KnownSnapshotRepository, ...]


def dashboard_page_html(
    target: Path,
    config: AppConfig,
    *,
    shell_page_html: ShellPageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
    program_root: Path | None = None,
) -> str:
    summary = dashboard_summary(target, program_root=program_root)
    actions = dashboard_actions(summary)
    return shell_page_html(
        "Dashboard",
        f"""
        <nav class="subnav">
          <a href="/settings">Innstillinger</a>
          <a href="/sources">Importerte mapper</a>
          <a href="/settings/removed">Slettede bilder</a>
          <a href="/people">Personer</a>
        </nav>
        <section class="dashboard-section" aria-labelledby="dashboard-actions-heading">
          <h2 id="dashboard-actions-heading">Anbefalte handlinger</h2>
          <div class="dashboard-actions">
            {"".join(dashboard_action_html(action) for action in actions)}
          </div>
        </section>
        <section class="dashboard-grid" aria-label="Status">
          {overview_section_html(summary)}
          {control_section_html(summary)}
          {snapshot_repositories_section_html(summary)}
          {coverage_section_html(summary)}
        </section>
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def dashboard_summary(
    target: Path,
    *,
    program_root: Path | None = None,
) -> DashboardSummary:
    conn = db.connect(target)
    try:
        status_counts = db.status_counts(conn)
        total_active = cast(int, status_counts["total"])
        media_counts = cast(dict[str, int], status_counts["media"])
        date_source_counts = cast(dict[str, int], status_counts["date_sources"])
        collection_id = db.validate_collection_id(conn)
        snapshot_repositories = dashboard_snapshot_repositories(
            program_root or Path(__file__).resolve().parents[1],
            collection_id,
        )
        size_totals = collection_size_totals(conn)
        return DashboardSummary(
            total_active=int(total_active),
            total_active_size_bytes=size_totals["active"],
            active_images=int(media_counts.get("bilder", 0)),
            active_image_size_bytes=size_totals["images"],
            active_videos=int(media_counts.get("videoer", 0)),
            active_video_size_bytes=size_totals["videos"],
            deleted_files=count_deleted_files(conn),
            deleted_file_size_bytes=size_totals["deleted"],
            source_status_counts=source_status_counts(conn),
            source_file_count=count_table_rows(conn, "file_sources"),
            duplicate_source_count=db.duplicate_source_count(conn),
            unresolved_errors=db.error_count(conn),
            name_conflicts=count_name_conflicts(conn),
            undated_files=count_undated_files(conn),
            pending_file_moves=len(db.prepared_pending_file_moves(conn)),
            date_source_counts={str(key): int(value) for key, value in date_source_counts.items()},
            geo_stats=db.geo_stats(conn),
            snapshot_repositories=snapshot_repositories,
        )
    finally:
        conn.close()


def count_table_rows(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def count_deleted_files(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM files WHERE deleted_at IS NOT NULL").fetchone()[0]
    )


def collection_size_totals(conn: sqlite3.Connection) -> dict[str, int]:
    totals = {"active": 0, "images": 0, "videos": 0, "deleted": 0}
    for row in conn.execute("SELECT stored_filename, size_bytes, deleted_at FROM files"):
        size_bytes = int(row["size_bytes"])
        if row["deleted_at"] is not None:
            totals["deleted"] += size_bytes
            continue
        totals["active"] += size_bytes
        suffix = Path(str(row["stored_filename"])).suffix.lower()
        totals["videos" if suffix in VIDEO_EXTENSIONS else "images"] += size_bytes
    return totals


def source_status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        str(row["status"]): int(row["count"])
        for row in conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM sources
            GROUP BY status
            ORDER BY status
            """
        )
    }


def count_name_conflicts(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM files
            WHERE deleted_at IS NULL
              AND name_conflict = 1
            """
        ).fetchone()[0]
    )


def count_undated_files(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM files
            WHERE deleted_at IS NULL
              AND (taken_date IS NULL OR date_source = 'unknown')
            """
        ).fetchone()[0]
    )


def dashboard_actions(summary: DashboardSummary) -> tuple[DashboardAction, ...]:
    actions: list[DashboardAction] = []

    if summary.unresolved_errors:
        actions.append(
            DashboardAction(
                "Uløste feil",
                "kritisk",
                f"{summary.unresolved_errors} feil må sjekkes.",
                "bildebank errors",
                "/help/errors.md",
            )
        )
    if summary.pending_file_moves:
        actions.append(
            DashboardAction(
                "Mulige integritetsproblemer",
                "kritisk",
                f"{summary.pending_file_moves} filflyttinger er uavklarte.",
                "bildebank doctor",
                "/help/doctor.md",
            )
        )
    if summary.undated_files:
        actions.append(
            DashboardAction(
                "Filer uten dato",
                "bør gjøres",
                f"{summary.undated_files} filer mangler trygg dato.",
                "bildebank refresh-metadata",
                "/help/refresh-metadata.md",
            )
        )

    actions.extend(
        (
            scan_action(
                "geo-scan",
                fact_rows=(
                    ("Manuell H3", str(summary.geo_stats["manual_h3"])),
                    ("Feil", str(summary.geo_stats["errors"])),
                ),
            ),
            scan_action("face-scan"),
            scan_action("image-scan"),
        )
    )
    actions.append(
        DashboardAction(
            "Thumbnails",
            "valgfritt",
            'I Bildebank-vinduet kan du trykke "Lag miniatyrbilder".',
            None,
            "/help/make-thumbnails.md",
            thumbnail_maintenance=True,
        )
    )

    if not any(action.severity != "oppdatert" for action in actions):
        actions.append(DashboardAction("Status", "oppdatert", "Ingen kjente mangler i dashboard-tallene."))
    return tuple(actions)


def scan_action(name: str, *, fact_rows: tuple[tuple[str, str], ...] = ()) -> DashboardAction:
    button_labels = {
        "geo-scan": "Les GPS fra bilder",
        "face-scan": "Finn ansikter",
        "image-scan": "Klargjør bildesøk",
    }
    return DashboardAction(
        name,
        "bør gjøres",
        "Oppdaterer tall...",
        None,
        f"/help/{name}.md",
        maintenance_name=name,
        gui_label=button_labels.get(name),
        fact_rows=fact_rows,
    )


def overview_section_html(summary: DashboardSummary) -> str:
    source_rows = "".join(
        info_row_html(f"Kilder: {status}", str(count)) for status, count in summary.source_status_counts.items()
    )
    if not source_rows:
        source_rows = info_row_html("Kilder", "0")
    return dashboard_card_html(
        "Samlingsoversikt",
        f"""
        <dl class="info-list">
          {info_row_html("Aktive filer", count_and_size(summary.total_active, summary.total_active_size_bytes))}
          {info_row_html("Bilder", count_and_size(summary.active_images, summary.active_image_size_bytes))}
          {info_row_html("Videoer", count_and_size(summary.active_videos, summary.active_video_size_bytes))}
          {info_row_html("Slettede bilder", count_and_size(summary.deleted_files, summary.deleted_file_size_bytes))}
          {source_rows}
          {info_row_html("Registrerte kildefiler", str(summary.source_file_count))}
          {info_row_html("Duplikatkilder", str(summary.duplicate_source_count))}
        </dl>
        """,
    )


def count_and_size(count: int, size_bytes: int) -> str:
    return f"{count} ({format_bytes(size_bytes)})"


def control_section_html(summary: DashboardSummary) -> str:
    return dashboard_card_html(
        "Kontrollstatus",
        f"""
        <dl class="info-list">
          {info_row_html("Uløste feil", str(summary.unresolved_errors), "/help/errors.md")}
          {info_row_html(
              "Navnekollisjoner",
              str(summary.name_conflicts),
              "/help/show-conflict.md",
              title=(
                  "Ufarlig: Bildebank er designet for å håndtere dette. "
                  "Ved import får ulike filer med samme navn et nytt lagret filnavn."
              ),
          )}
          {info_row_html("Filer uten dato", str(summary.undated_files), "/help/refresh-metadata.md")}
          {info_row_html("Uavklarte filflyttinger", str(summary.pending_file_moves), "/help/doctor.md")}
        </dl>
        """,
    )


def coverage_section_html(summary: DashboardSummary) -> str:
    date_source_rows = "".join(
        info_row_html(f"Dato: {source}", str(count)) for source, count in sorted(summary.date_source_counts.items())
    )
    maintenance_rows = "".join(maintenance_info_row_html(name) for name in ("face-scan", "geo-scan", "image-scan"))
    return dashboard_card_html(
        "Dekning",
        f"""
        <dl class="info-list">
          {date_source_rows or info_row_html("Dato", "Ingen aktive filer")}
          {info_row_html("GPS scannet", f'{summary.geo_stats["scanned"]} av {summary.geo_stats["total"]}')}
          {info_row_html("GPS funnet", str(summary.geo_stats["with_gps"]))}
          {info_row_html("GPS uten treff", str(summary.geo_stats["without_gps"]))}
          {info_row_html("GPS-feil", str(summary.geo_stats["errors"]))}
          {maintenance_rows}
          {thumbnail_coverage_info_row_html()}
        </dl>
        """,
    )


def dashboard_snapshot_repositories(
    program_root: Path,
    collection_id: str,
) -> tuple[KnownSnapshotRepository, ...]:
    try:
        return tuple(known_snapshot_repositories(program_root, collection_id))
    except (OSError, sqlite3.Error):
        return ()


def snapshot_repositories_section_html(summary: DashboardSummary) -> str:
    if summary.snapshot_repositories:
        rows = "".join(
            info_row_html(
                snapshot_repository_label(repository),
                snapshot_repository_status_text(repository),
            )
            for repository in summary.snapshot_repositories
        )
    else:
        rows = info_row_html(
            "Status",
            "Ingen publiserte snapshots er registrert på denne installasjonen.",
        )
    return dashboard_card_html(
        "Snapshots",
        f"""
        <dl class="info-list">
          {rows}
        </dl>
        <p><a href="/help/snapshot.md">Hjelp om snapshots</a></p>
        """,
    )


def snapshot_repository_label(repository: KnownSnapshotRepository) -> str:
    folder_name = repository.path.name or str(repository.path)
    return f"{folder_name} ({repository.repository_id[:8]})"


def snapshot_repository_status_text(repository: KnownSnapshotRepository) -> str:
    status = {
        "complete": "complete – uten kjente avvik",
        "degraded": "degraded – publisert med problemer",
        "recovery": "recovery – kan ikke brukes som vanlig hel restore",
    }.get(repository.last_snapshot_status, repository.last_snapshot_status)
    return (
        f"{status}; sist publisert {format_snapshot_time(repository.last_snapshot_at)}; "
        f"{repository.path}"
    )


def format_snapshot_time(value: str) -> str:
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc
        )
    except ValueError:
        return value
    return parsed.astimezone().strftime("%d.%m.%Y kl. %H:%M")


def dashboard_card_html(title: str, content: str) -> str:
    return f"""
    <section class="dashboard-card">
      <h2>{html.escape(title)}</h2>
      {content}
    </section>
    """


def info_row_html(label: str, value: str, href: str | None = None, *, title: str | None = None) -> str:
    rendered_value = html.escape(value)
    if href:
        title_attr = f' title="{html.escape(title)}"' if title else ""
        rendered_value = f'<a href="{html.escape(href)}"{title_attr}>{rendered_value}</a>'
    return f"""
    <div class="info-row">
      <dt>{html.escape(label)}</dt>
      <dd>{rendered_value}</dd>
    </div>
    """


def maintenance_info_row_html(name: str) -> str:
    return f"""
    <div class="info-row" data-maintenance-name="{html.escape(name)}">
      <dt>{html.escape(name)}</dt>
      <dd>
        <a href="/help/{html.escape(name)}.md" data-maintenance-coverage-status>
          <span data-maintenance-current>-</span> av <span data-maintenance-total>-</span>
        </a>
      </dd>
    </div>
    """


def thumbnail_coverage_info_row_html() -> str:
    return """
    <div class="info-row">
      <dt>Thumbnails</dt>
      <dd>
        <a href="/help/make-thumbnails.md" data-thumbnail-coverage-status>
          Telles under Anbefalte handlinger
        </a>
      </dd>
    </div>
    """


def dashboard_action_html(action: DashboardAction) -> str:
    command_html = f'<code>{html.escape(action.command)}</code>' if action.command else ""
    command_class = " dashboard-action-has-command" if action.command else ""
    help_html = f'<a href="{html.escape(action.help_path)}">Hjelp</a>' if action.help_path else ""
    link_html = f'<a href="{html.escape(action.link_path)}">Åpne</a>' if action.link_path else ""
    links = " ".join(part for part in (help_html, link_html) if part)
    severity_class = {
        "kritisk": "critical",
        "bør gjøres": "recommended",
        "valgfritt": "optional",
        "oppdatert": "current",
    }.get(action.severity, "optional")
    maintenance_attr = (
        f' data-maintenance-name="{html.escape(action.maintenance_name)}"'
        f' data-maintenance-gui-label="{html.escape(action.gui_label)}"'
        if action.maintenance_name and action.gui_label
        else ""
    )
    thumbnail_attr = " data-thumbnail-maintenance" if action.thumbnail_maintenance else ""
    detail_attr = " data-maintenance-status" if action.maintenance_name else ""
    counts_html = (
        """
      <dl class="maintenance-counts">
        <div><dt>Scannet</dt><dd data-maintenance-current>-</dd></div>
        <div><dt>Mangler</dt><dd data-maintenance-missing>-</dd></div>
        <div><dt>Totalt</dt><dd data-maintenance-total>-</dd></div>
      </dl>
        """
        if action.maintenance_name
        else ""
    )
    facts_html = (
        '<dl class="maintenance-counts">'
        + "".join(
            f"<div><dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd></div>"
            for label, value in action.fact_rows
        )
        + "</dl>"
        if action.fact_rows
        else ""
    )
    thumbnail_counts_html = (
        """
      <p class="status" data-thumbnail-status>Ikke telt ennå</p>
      <button type="button" class="maintenance-action" title="Sjekk på disk hvor mange bilder som har thumbnails" data-count-thumbnails>Tell thumbnails</button>
      <dl class="maintenance-counts">
        <div><dt>Oppdatert</dt><dd data-thumbnail-current>-</dd></div>
        <div><dt>Mangler</dt><dd data-thumbnail-missing>-</dd></div>
        <div><dt>Totalt</dt><dd data-thumbnail-total>-</dd></div>
      </dl>
        """
        if action.thumbnail_maintenance
        else ""
    )
    return f"""
    <article class="dashboard-action dashboard-action-{severity_class}{command_class}"{maintenance_attr}{thumbnail_attr}>
      <div>
        <h3>{html.escape(action.title)}</h3>
        <p{detail_attr}>{html.escape(action.detail)}</p>
      </div>
      <strong>{html.escape(action.severity)}</strong>
      {command_html}
      <div class="dashboard-action-links">{links}</div>
      {counts_html}
      {facts_html}
      {thumbnail_counts_html}
    </article>
    """
