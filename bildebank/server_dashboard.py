from __future__ import annotations

import html
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import db, server_app
from .config import AppConfig


ShellPageRenderer = Callable[..., str]


@dataclass(frozen=True)
class DashboardAction:
    title: str
    severity: str
    detail: str
    command: str | None = None
    help_path: str | None = None
    link_path: str | None = None


@dataclass(frozen=True)
class DashboardSummary:
    total_active: int
    active_images: int
    active_videos: int
    deleted_files: int
    source_status_counts: dict[str, int]
    source_file_count: int
    duplicate_source_count: int
    unresolved_errors: int
    name_conflicts: int
    undated_files: int
    pending_file_moves: int
    date_source_counts: dict[str, int]
    geo_stats: dict[str, int]
    maintenance_statuses: tuple[server_app.MaintenanceStatus, ...]


def dashboard_page_html(
    target: Path,
    config: AppConfig,
    *,
    shell_page_html: ShellPageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    summary = dashboard_summary(target, config)
    actions = dashboard_actions(summary)
    return shell_page_html(
        "Dashboard",
        f"""
        <nav class="subnav">
          <a href="/settings">Innstillinger</a>
          <a href="/sources">Importerte mapper</a>
          <a href="/settings/removed">Slettede bilder</a>
          <a href="/geo/stats">GPS-statistikk</a>
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
          {coverage_section_html(summary)}
        </section>
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def dashboard_summary(target: Path, config: AppConfig) -> DashboardSummary:
    conn = db.connect(target)
    try:
        status_counts = db.status_counts(conn)
        media_counts = dict(status_counts["media"])
        return DashboardSummary(
            total_active=int(status_counts["total"]),
            active_images=int(media_counts.get("bilder", 0)),
            active_videos=int(media_counts.get("videoer", 0)),
            deleted_files=len(list(db.deleted_files(conn))),
            source_status_counts=source_status_counts(conn),
            source_file_count=count_table_rows(conn, "file_sources"),
            duplicate_source_count=db.duplicate_source_count(conn),
            unresolved_errors=db.error_count(conn),
            name_conflicts=count_name_conflicts(conn),
            undated_files=count_undated_files(conn),
            pending_file_moves=len(db.prepared_pending_file_moves(conn)),
            date_source_counts={str(key): int(value) for key, value in dict(status_counts["date_sources"]).items()},
            geo_stats=db.geo_stats(conn),
            maintenance_statuses=server_app.maintenance_statuses(target, config),
        )
    finally:
        conn.close()


def count_table_rows(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


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
    statuses = {status.name: status for status in summary.maintenance_statuses}
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

    actions.extend(scan_action(statuses.get(name)) for name in ("geo-scan", "face-scan", "image-scan"))
    actions.append(
        DashboardAction(
            "Thumbnails",
            "valgfritt",
            "Bruk eksisterende thumbnail-telling for å se om nettleservisningen kan gjøres raskere.",
            "bildebank make-thumbnails",
            "/help/make-thumbnails.md",
        )
    )
    actions.append(
        DashboardAction(
            "Backup",
            "bør gjøres",
            "Ta backup av bildesamlingen og deleted/ før større opprydding.",
            r"bildebank backup --dry-run D:\Backuper",
            "/help/backup.md",
        )
    )

    if not any(action.severity != "oppdatert" for action in actions):
        actions.append(DashboardAction("Status", "oppdatert", "Ingen kjente mangler i dashboard-tallene."))
    return tuple(actions)


def scan_action(status: server_app.MaintenanceStatus | None) -> DashboardAction:
    if status is None:
        return DashboardAction("Scan", "oppdatert", "Oppdatert")
    if status.missing == 0:
        return DashboardAction(status.name, "oppdatert", "Oppdatert", help_path=status.help_path)
    return DashboardAction(
        status.name,
        "bør gjøres",
        f"{status.missing} av {status.total} bilder mangler {status.name}.",
        f"bildebank {status.name}",
        status.help_path,
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
          {info_row_html("Aktive filer", str(summary.total_active))}
          {info_row_html("Bilder", str(summary.active_images))}
          {info_row_html("Videoer", str(summary.active_videos))}
          {info_row_html("Slettede bilder", str(summary.deleted_files))}
          {source_rows}
          {info_row_html("Registrerte kildefiler", str(summary.source_file_count))}
          {info_row_html("Duplikatkilder", str(summary.duplicate_source_count))}
        </dl>
        """,
    )


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
    maintenance_rows = "".join(
        info_row_html(
            status.name,
            "Oppdatert" if status.missing == 0 else f"{status.missing} mangler av {status.total}",
            status.help_path,
        )
        for status in summary.maintenance_statuses
    )
    return dashboard_card_html(
        "Dekning",
        f"""
        <dl class="info-list">
          {date_source_rows or info_row_html("Dato", "Ingen aktive filer")}
          {info_row_html("GPS scannet", f'{summary.geo_stats["scanned"]} av {summary.geo_stats["total"]}', "/geo/stats")}
          {info_row_html("GPS funnet", str(summary.geo_stats["with_gps"]), "/geo/stats")}
          {info_row_html("GPS uten treff", str(summary.geo_stats["without_gps"]), "/geo/stats")}
          {info_row_html("GPS-feil", str(summary.geo_stats["errors"]), "/geo/stats")}
          {maintenance_rows}
          {info_row_html("Thumbnails", "Telles via /api/maintenance/thumbnails", "/help/make-thumbnails.md")}
        </dl>
        """,
    )


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


def dashboard_action_html(action: DashboardAction) -> str:
    command_html = f'<code>{html.escape(action.command)}</code>' if action.command else ""
    help_html = f'<a href="{html.escape(action.help_path)}">Hjelp</a>' if action.help_path else ""
    link_html = f'<a href="{html.escape(action.link_path)}">Åpne</a>' if action.link_path else ""
    links = " ".join(part for part in (help_html, link_html) if part)
    severity_class = {
        "kritisk": "critical",
        "bør gjøres": "recommended",
        "valgfritt": "optional",
        "oppdatert": "current",
    }.get(action.severity, "optional")
    return f"""
    <article class="dashboard-action dashboard-action-{severity_class}">
      <div>
        <h3>{html.escape(action.title)}</h3>
        <p>{html.escape(action.detail)}</p>
      </div>
      <strong>{html.escape(action.severity)}</strong>
      {command_html}
      <div class="dashboard-action-links">{links}</div>
    </article>
    """
