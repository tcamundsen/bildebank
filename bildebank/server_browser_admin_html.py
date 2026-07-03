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
        f'<div class="people-table">{rows}</div>'
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
    items = "\n".join(tag_row_html(row) for row in rows)
    content = f'<div class="people-table">{items}</div>' if items else '<p class="meta">Ingen tagger registrert.</p>'
    return shell_page_html(
        "Tagger",
        f"""
        <h1>Tagger</h1>
        <form action="/tags/create" method="post" class="new-person-form">
          <label>Ny tagg <input name="name" autocomplete="off"></label>
          <button type="submit">Legg til</button>
        </form>
        {content}
        """,
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def tag_row_html(row: sqlite3.Row) -> str:
    tag_id = int(row["id"])
    name = str(row["name"])
    kind = str(row["kind"])
    kind_label = "systemtagg" if kind == db.TAG_KIND_SYSTEM else "brukertagg"
    url = "/tag/" + urllib.parse.quote(name, safe="")
    actions = tag_row_actions_html(tag_id, name, kind)
    return f"""
    <div class="people-row">
      <div class="people-name">{html.escape(name)}</div>
      <a class="person-link" href="{html.escape(url)}">Vis bilder ({int(row["file_count"])})</a>
      <span class="status">{html.escape(kind_label)}</span>
      <span class="status">opprettet: {html.escape(str(row["created_at"]))}</span>
      {actions}
    </div>
    """


def tag_row_actions_html(tag_id: int, name: str, kind: str) -> str:
    if kind == db.TAG_KIND_SYSTEM:
        return '<span class="status">systemtagg kan ikke endres</span>'
    escaped_name = html.escape(name)
    return f"""
      <div class="tag-actions">
        <form action="/tags/rename" method="post" class="inline-edit-form">
          <input type="hidden" name="tag_id" value="{tag_id}">
          <input name="name" value="{escaped_name}" autocomplete="off" aria-label="Nytt taggnavn">
          <button type="submit">Endre navn</button>
        </form>
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
    source_file_count = int(source["source_file_count"])
    imported_at = str(source["imported_at"] or "-")
    source_browser = imported_source_browser_source(source)
    return f"""
    <div class="people-row">
      <div class="people-name">{html.escape(name)}</div>
      <a class="person-link" href="{html.escape(source_browser.root_url)}">Vis bilder ({active_file_count})</a>
      <span class="status">filer fra kilde: {source_file_count}</span>
      <span class="status">status: {html.escape(status)}</span>
      <span class="status">importert: {html.escape(imported_at)}</span>
      <div class="detail">{html.escape(str(source["path"]))}</div>
    </div>
    """
