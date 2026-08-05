from __future__ import annotations

import html
import sqlite3
import urllib.parse
from pathlib import Path
from typing import Callable

from . import db
from .server_browser_queries import source_summary_rows
from .server_browser_sources import imported_source_browser_source


ShellPageRenderer = Callable[..., str]


def sources_page_html(
    target: Path,
    *,
    shell_page_html: ShellPageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    sources = source_summary_rows(target)
    rows = "\n".join(source_row_html(source) for source in sources)
    content = (
        f"""
        <div class="sources-table">
          <div class="source-header" aria-hidden="true">
            <span>Mappe &amp; sti</span>
            <span>Filer totalt</span>
            <span>Bare i denne</span>
            <span>Status</span>
            <span>Importert</span>
            <span>Handling</span>
          </div>
          {rows}
        </div>
        """
        if rows
        else '<p class="meta">Ingen importerte kilder registrert.</p>'
    )
    return shell_page_html(
        "Importerte mapper",
        f"""
        <h1>Importerte mapper</h1>
        <p>Denne siden viser alle importerte mapper, dvs samme info som du får fra å kjøre
        <code>bildebank list-sources</code></p>
        {content}
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def tags_page_html(
    target: Path,
    *,
    shell_page_html: ShellPageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    conn = db.connect(target)
    try:
        rows = list(db.tags(conn))
    finally:
        conn.close()
    user_rows = [row for row in rows if row["kind"] == db.TAG_KIND_USER]
    system_rows = [
        row
        for row in rows
        if row["kind"] == db.TAG_KIND_SYSTEM
        and (
            row["system_key"] != db.SYSTEM_TAG_DUPLICATE_REPAIR_REVIEW_KEY
            or int(row["file_count"]) > 0
        )
    ]
    user_content = (
        f"""
        <div class="user-tag-table">
          <div class="user-tag-header" aria-hidden="true">
            <span>Tagg</span>
            <span>Bilder</span>
            <span>Opprettet</span>
            <span>Handlinger</span>
          </div>
          {"\n".join(user_tag_row_html(row) for row in user_rows)}
        </div>
        """
        if user_rows
        else '<p class="meta">Ingen brukertagger registrert.</p>'
    )
    system_content = (
        f"""
        <div class="system-tag-table">
          <div class="system-tag-header" aria-hidden="true">
            <span>Tagg</span>
            <span>Bilder</span>
          </div>
          {"\n".join(system_tag_row_html(row) for row in system_rows)}
        </div>
        """
        if system_rows
        else '<p class="meta">Ingen systemtagger registrert.</p>'
    )
    return shell_page_html(
        "Tagger",
        f"""
        <h1>Tagger</h1>
        <form action="/tags/create" method="post" class="new-person-form">
          <label>Ny tagg <input name="name" autocomplete="off"></label>
          <button type="submit">Legg til</button>
        </form>
        <section class="tag-section">
          <h2>Brukertagger</h2>
          {user_content}
        </section>
        <section class="tag-section">
          <h2>Systemtagger</h2>
          {system_content}
        </section>
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def system_tag_row_html(row: sqlite3.Row) -> str:
    name = str(row["name"])
    image_count = int(row["file_count"])
    image_count_label = "1 bilde" if image_count == 1 else f"{image_count} bilder"
    url = "/tag/" + urllib.parse.quote(name, safe="")
    return f"""
    <div class="system-tag-row">
      <div class="people-name">{html.escape(name)}</div>
      <a class="person-link" href="{html.escape(url)}">{image_count_label}</a>
    </div>
    """


def user_tag_row_html(row: sqlite3.Row) -> str:
    tag_id = int(row["id"])
    name = str(row["name"])
    image_count = int(row["file_count"])
    image_count_label = "1 bilde" if image_count == 1 else f"{image_count} bilder"
    url = "/tag/" + urllib.parse.quote(name, safe="")
    return f"""
    <div class="user-tag-row">
      <div class="user-tag-field">
        <span class="user-tag-field-label">Tagg</span>
        <div class="people-name">{html.escape(name)}</div>
      </div>
      <div class="user-tag-field">
        <span class="user-tag-field-label">Bilder</span>
        <a class="person-link" href="{html.escape(url)}">{image_count_label}</a>
      </div>
      <div class="user-tag-field">
        <span class="user-tag-field-label">Opprettet</span>
        <span>{html.escape(str(row["created_at"]))}</span>
      </div>
      <div class="user-tag-field user-tag-action">
        <span class="user-tag-field-label">Handlinger</span>
        {tag_row_actions_html(tag_id, name)}
      </div>
    </div>
    """


def tag_row_actions_html(tag_id: int, name: str) -> str:
    escaped_name = html.escape(name)
    return f"""
      <div class="tag-actions">
        <details class="tag-rename-details">
          <summary class="tag-rename-toggle">Endre navn</summary>
          <form action="/tags/rename" method="post" class="inline-edit-form tag-rename-form">
            <input type="hidden" name="tag_id" value="{tag_id}">
            <input name="name" value="{escaped_name}" autocomplete="off" aria-label="Nytt taggnavn">
            <button type="submit">Lagre</button>
          </form>
        </details>
        <form action="/tags/delete" method="post">
          <input type="hidden" name="tag_id" value="{tag_id}">
          <button type="submit" class="danger-button" data-confirm-submit="Slette taggen {escaped_name} fra alle bilder?">Slett</button>
        </form>
      </div>
    """


def source_row_html(source: sqlite3.Row) -> str:
    name = str(source["name"])
    status = str(source["status"])
    active_file_count = int(source["active_file_count"])
    exclusive_active_file_count = int(source["exclusive_active_file_count"])
    source_file_count = int(source["source_file_count"])
    imported_at = str(source["imported_at"] or "-")
    source_browser = imported_source_browser_source(source)
    status_label = source_status_label(status)
    active_file_label = "1 bilde" if active_file_count == 1 else f"{active_file_count} bilder"
    return f"""
    <div class="source-row">
      <div class="source-field source-folder">
        <span class="source-field-label">Mappe &amp; sti</span>
        <div class="source-name">{html.escape(name)}</div>
        <div class="source-path">{html.escape(str(source["path"]))}</div>
      </div>
      <div class="source-field">
        <span class="source-field-label">Filer totalt</span>
        <span>{source_file_count}</span>
      </div>
      <div class="source-field">
        <span class="source-field-label">Bare i denne</span>
        <span>{exclusive_active_file_count}</span>
      </div>
      <div class="source-field">
        <span class="source-field-label">Status</span>
        <span>{html.escape(status_label)}</span>
      </div>
      <div class="source-field">
        <span class="source-field-label">Importert</span>
        <span>{html.escape(imported_at)}</span>
      </div>
      <div class="source-field source-action">
        <span class="source-field-label">Handling</span>
        <a class="person-link" href="{html.escape(source_browser.root_url)}">{active_file_label}</a>
      </div>
    </div>
    """


def source_status_label(status: str) -> str:
    return {
        "imported": "Importert",
        "pending": "Venter",
        "dry-run": "Testkjøring",
        "superseded": "Erstattet",
    }.get(status, status)
