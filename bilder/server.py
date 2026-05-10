from __future__ import annotations

import html
import mimetypes
import os
import threading
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import db
from .config import OpenClipConfig
from .html_export import display_relative_path, format_bytes, month_key_from_path
from .openclip import (
    ImageSearchResult,
    connect_openclip_db,
    cosine_similarity,
    create_search_run,
    embedding_from_blob,
    load_text_model,
    relative_to_target,
    text_embedding,
)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_SEARCH_LIMIT = 100


@dataclass(frozen=True)
class ServerSearchStats:
    query: str
    results: tuple[ImageSearchResult, ...]


class OpenClipSearchCache:
    def __init__(self, config: OpenClipConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._model: Any | None = None
        self._tokenizer: Any | None = None

    def text_vector(self, query: str) -> list[float]:
        with self._lock:
            if self._model is None or self._tokenizer is None:
                self._model, self._tokenizer = load_text_model(self.config)
            return text_embedding(self._model, self._tokenizer, query)

    @property
    def loaded(self) -> bool:
        return self._model is not None and self._tokenizer is not None


class BildebankServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], target: Path, config: OpenClipConfig) -> None:
        super().__init__(address, BildebankRequestHandler)
        self.target = target
        self.config = config
        self.search_cache = OpenClipSearchCache(config)


class BildebankRequestHandler(BaseHTTPRequestHandler):
    server: BildebankServer

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        try:
            if parsed.path == "/":
                self.respond_browser_root()
                return
            if parsed.path.startswith("/item/"):
                self.respond_item(parsed.path.removeprefix("/item/"))
                return
            if parsed.path.startswith("/month/"):
                self.respond_month(parsed.path.removeprefix("/month/"))
                return
            if parsed.path == "/search":
                params = urllib.parse.parse_qs(parsed.query)
                query = first_param(params, "q").strip()
                limit = positive_int_param(params, "limit", DEFAULT_SEARCH_LIMIT)
                if not query:
                    self.respond_html(index_html(self.server, message="Skriv inn et søk."))
                    return
                stats = search_server_images(self.server, query=query, limit=limit)
                self.respond_html(search_html(self.server, stats, limit))
                return
            if parsed.path.startswith("/file/"):
                self.respond_file(parsed.path.removeprefix("/file/"))
                return
            self.respond_file(parsed.path.lstrip("/"))
        except Exception as exc:  # noqa: BLE001 - local server should show readable errors
            self.respond_html(error_html(exc), status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def respond_html(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.respond_bytes(content.encode("utf-8"), "text/html; charset=utf-8", status=status)

    def respond_text(self, content: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.respond_bytes(content.encode("utf-8"), "text/plain; charset=utf-8", status=status)

    def respond_bytes(self, content: bytes, content_type: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.end_headers()

    def respond_browser_root(self) -> None:
        item = first_browser_item(self.server.target)
        if item is None:
            self.respond_html(empty_browser_html())
            return
        self.redirect(f"/item/{item['id']}")

    def respond_item(self, raw_file_id: str) -> None:
        file_id = parse_file_id(raw_file_id)
        item = browser_item_by_id(self.server.target, file_id)
        if item is None:
            self.respond_text("Filen finnes ikke i bildesamlingen.", status=HTTPStatus.NOT_FOUND)
            return
        previous_item, next_item = adjacent_browser_items(self.server.target, item)
        month_nav = browser_month_navigation(self.server.target, item)
        self.respond_html(item_page_html(self.server.target, item, previous_item, next_item, month_nav))

    def respond_month(self, raw_month: str) -> None:
        month_key = urllib.parse.unquote(raw_month).strip()
        if not valid_month_key(month_key):
            self.respond_text("Ugyldig måned.", status=HTTPStatus.BAD_REQUEST)
            return
        items = browser_month_items(self.server.target, month_key)
        self.respond_html(month_page_html(self.server.target, month_key, items))

    def respond_file(self, encoded_relative_path: str) -> None:
        raw_path = urllib.parse.unquote(encoded_relative_path).strip("/")
        if raw_path.isdigit():
            row = browser_item_by_id(self.server.target, int(raw_path))
            if row is None:
                self.respond_text("Filen finnes ikke.", status=HTTPStatus.NOT_FOUND)
                return
            path = Path(str(row["target_path"]))
        else:
            relative = Path(raw_path)
            path = (self.server.target / relative).resolve()
            try:
                path.relative_to(self.server.target.resolve())
            except ValueError:
                self.respond_text("Ugyldig filsti.", status=HTTPStatus.FORBIDDEN)
                return
        if not path.is_file():
            self.respond_text("Filen finnes ikke.", status=HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        try:
            content = path.read_bytes()
        except OSError as exc:
            self.respond_text(str(exc), status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.respond_bytes(content, content_type)


def first_param(params: dict[str, list[str]], name: str) -> str:
    values = params.get(name, [])
    return values[0] if values else ""


def positive_int_param(params: dict[str, list[str]], name: str, default: int) -> int:
    raw = first_param(params, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def parse_file_id(value: str) -> int:
    try:
        file_id = int(value)
    except ValueError as exc:
        raise ValueError("Ugyldig file_id.") from exc
    if file_id < 1:
        raise ValueError("Ugyldig file_id.")
    return file_id


def first_browser_item(target: Path) -> Any | None:
    conn = db.connect(target)
    try:
        return conn.execute(
            """
            SELECT id, target_path, stored_filename, taken_date, date_source, size_bytes
            FROM files
            WHERE deleted_at IS NULL
            ORDER BY COALESCE(taken_date, ''), target_path
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()


def browser_item_by_id(target: Path, file_id: int) -> Any | None:
    conn = db.connect(target)
    try:
        return conn.execute(
            """
            SELECT id, target_path, stored_filename, taken_date, date_source, size_bytes
            FROM files
            WHERE deleted_at IS NULL AND id = ?
            """,
            (file_id,),
        ).fetchone()
    finally:
        conn.close()


def adjacent_browser_items(target: Path, item: Any) -> tuple[Any | None, Any | None]:
    key = item_order_key(item)
    conn = db.connect(target)
    try:
        previous_item = conn.execute(
            """
            SELECT id, target_path, stored_filename, taken_date, date_source, size_bytes
            FROM files
            WHERE deleted_at IS NULL
              AND (
                COALESCE(taken_date, '') < ?
                OR (COALESCE(taken_date, '') = ? AND target_path < ?)
              )
            ORDER BY COALESCE(taken_date, '') DESC, target_path DESC
            LIMIT 1
            """,
            (key[0], key[0], key[1]),
        ).fetchone()
        next_item = conn.execute(
            """
            SELECT id, target_path, stored_filename, taken_date, date_source, size_bytes
            FROM files
            WHERE deleted_at IS NULL
              AND (
                COALESCE(taken_date, '') > ?
                OR (COALESCE(taken_date, '') = ? AND target_path > ?)
              )
            ORDER BY COALESCE(taken_date, ''), target_path
            LIMIT 1
            """,
            (key[0], key[0], key[1]),
        ).fetchone()
        return previous_item, next_item
    finally:
        conn.close()


def item_order_key(item: Any) -> tuple[str, str]:
    return str(item["taken_date"] or ""), str(item["target_path"])


def browser_month_keys(target: Path) -> list[str]:
    conn = db.connect(target)
    try:
        rows = conn.execute(
            """
            SELECT target_path
            FROM files
            WHERE deleted_at IS NULL
            ORDER BY target_path
            """
        )
        keys = {
            month_key_from_path(Path(display_relative_path(target, Path(str(row["target_path"])))))
            for row in rows
        }
        return sorted(key for key in keys if valid_month_key(key))
    finally:
        conn.close()


def browser_month_navigation(target: Path, item: Any) -> dict[str, str | None]:
    current_key = month_key_from_path(
        Path(display_relative_path(target, Path(str(item["target_path"]))))
    )
    return browser_month_navigation_for_key(target, current_key)


def browser_month_navigation_for_key(target: Path, current_key: str) -> dict[str, str | None]:
    if not valid_month_key(current_key):
        return {
            "previous_year": None,
            "next_year": None,
            "previous_month": None,
            "next_month": None,
        }
    keys = browser_month_keys(target)
    years = sorted({key[:4] for key in keys})
    current_year = current_key[:4]
    current_year_index = years.index(current_year) if current_year in years else -1
    previous_year = years[current_year_index - 1] if current_year_index > 0 else None
    next_year = years[current_year_index + 1] if current_year_index < len(years) - 1 else None
    return {
        "previous_year": january_in_year(previous_year),
        "next_year": january_in_year(next_year),
        "previous_month": adjacent_calendar_month(current_key, -1),
        "next_month": adjacent_calendar_month(current_key, 1),
    }


def january_in_year(year: str | None) -> str | None:
    if year is None:
        return None
    return f"{year}-01"


def adjacent_calendar_month(month_key: str, delta: int) -> str:
    year_text, month_text = month_key.split("-", 1)
    year = int(year_text)
    month = int(month_text) + delta
    if month < 1:
        year -= 1
        month = 12
    elif month > 12:
        year += 1
        month = 1
    return f"{year:04d}-{month:02d}"


def browser_month_items(target: Path, month_key: str) -> list[Any]:
    year, month = month_key.split("-", 1)
    prefix = str((target / year / month).resolve())
    conn = db.connect(target)
    try:
        return list(
            conn.execute(
                """
                SELECT id, target_path, stored_filename, taken_date, date_source, size_bytes
                FROM files
                WHERE deleted_at IS NULL
                  AND target_path LIKE ?
                ORDER BY COALESCE(taken_date, ''), target_path
                """,
                (prefix + os.sep + "%",),
            )
        )
    finally:
        conn.close()


def valid_month_key(value: str) -> bool:
    if len(value) != 7 or value[4] != "-":
        return False
    year, month = value.split("-", 1)
    return year.isdigit() and month.isdigit() and 1 <= int(month) <= 12


def search_server_images(server: BildebankServer, *, query: str, limit: int) -> ServerSearchStats:
    clean_query = query.strip()
    if not clean_query:
        raise ValueError("Søketekst kan ikke være tom.")
    conn = connect_openclip_db(server.target)
    try:
        rows = list(
            conn.execute(
                """
                SELECT file_id, target_path, target_path_key, embedding
                FROM image_embeddings
                WHERE model_name = ? AND pretrained = ?
                """,
                (server.config.model_name, server.config.pretrained),
            )
        )
        if not rows:
            raise ValueError("Fant ingen bilde-embeddings. Kjør bildebank image-scan først.")
        text_vector = server.search_cache.text_vector(clean_query)
        scored = sorted(
            (
                (
                    cosine_similarity(text_vector, embedding_from_blob(bytes(row["embedding"]))),
                    int(row["file_id"]),
                    Path(str(row["target_path"])),
                    str(row["target_path_key"]),
                )
                for row in rows
            ),
            reverse=True,
            key=lambda item: item[0],
        )[:limit]
        run_id = create_search_run(conn, clean_query, server.config, limit)
        results: list[ImageSearchResult] = []
        for index, (score, file_id, target_path, target_path_key) in enumerate(scored, start=1):
            conn.execute(
                """
                INSERT INTO image_search_results(run_id, file_id, target_path, target_path_key, similarity, rank)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (run_id, file_id, str(target_path), target_path_key, score, index),
            )
            results.append(ImageSearchResult(index, file_id, target_path, score))
        conn.commit()
        return ServerSearchStats(clean_query, tuple(results))
    finally:
        conn.close()


def index_html(server: BildebankServer, *, message: str = "") -> str:
    if message:
        return search_start_html(server, message=message)
    item = first_browser_item(server.target)
    if item is None:
        return empty_browser_html()
    previous_item, next_item = adjacent_browser_items(server.target, item)
    month_nav = browser_month_navigation(server.target, item)
    return item_page_html(server.target, item, previous_item, next_item, month_nav)


def search_start_html(server: BildebankServer, *, message: str = "") -> str:
    return page_html(
        "Bildesøk",
        f"""
        <main class="shell">
          <p><a href="/">Til bildebrowser</a></p>
          <h1>Bildesøk</h1>
          <p class="meta">OpenCLIP {html.escape(server.config.model_name)} ({html.escape(server.config.pretrained)})</p>
          {message_html(message)}
          {search_form("")}
        </main>
        """,
    )


def search_html(server: BildebankServer, stats: ServerSearchStats, limit: int) -> str:
    items = "\n".join(result_html(server.target, result) for result in stats.results)
    return page_html(
        f"Bildesøk: {stats.query}",
        f"""
        <main class="shell">
          <p><a href="/">Til bildebrowser</a></p>
          <h1>Bildesøk</h1>
          {search_form(stats.query, limit)}
          <p class="meta">{len(stats.results)} treff. Sortert med beste match først. Modell lastet: {'ja' if server.search_cache.loaded else 'nei'}.</p>
          <div class="grid">
            {items}
          </div>
        </main>
        """,
    )


def error_html(exc: Exception) -> str:
    return page_html(
        "Feil",
        f"""
        <main class="shell">
          <h1>Feil</h1>
          <p class="error">{html.escape(str(exc))}</p>
          {search_form("")}
        </main>
        """,
    )


def search_form(query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> str:
    return f"""
    <form action="/search" method="get" class="search">
      <input name="q" value="{html.escape(query)}" placeholder="a photo of a beach" autofocus>
      <input name="limit" value="{limit}" inputmode="numeric" aria-label="Antall treff">
      <button type="submit">Søk</button>
    </form>
    """


def message_html(message: str) -> str:
    if not message:
        return ""
    return f'<p class="message">{html.escape(message)}</p>'


def result_html(target: Path, result: ImageSearchResult) -> str:
    relative = relative_to_target(target, result.target_path)
    url = f"/file/{result.file_id}"
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


def empty_browser_html() -> str:
    return page_html(
        "Bildebrowser",
        """
        <main class="shell">
          <h1>Bildebrowser</h1>
          <p class="meta">Ingen filer i bildesamlingen.</p>
          <p><a href="/search">Bildesøk</a></p>
        </main>
        """,
    )


def item_page_html(
    target: Path,
    item: Any,
    previous_item: Any | None,
    next_item: Any | None,
    month_nav: dict[str, str | None],
) -> str:
    target_path = Path(str(item["target_path"]))
    relative = display_relative_path(target, target_path)
    month_key = month_key_from_path(Path(relative))
    media = item_media_html(item)
    controls = browser_controls_html(month_nav, previous_item, next_item)
    return page_html(
        f"Bildebrowser: {target_path.name}",
        f"""
        <main class="server-browser">
          <header class="browser-header">
            <div class="topline">
              <div class="title">Bildebrowser</div>
              <span class="status">{html.escape(relative)} · {html.escape(format_bytes(int(item["size_bytes"])))} · {html.escape(str(item["date_source"]))}</span>
              <a class="server-search-link" href="/search">Bildesøk</a>
            </div>
            {controls}
          </header>
          <section class="stage">{media}</section>
          <footer class="browser-footer">
            <a class="filename" href="/file/{int(item["id"])}" target="_blank">{html.escape(relative)}</a>
            {month_overview_link(month_key)}
          </footer>
        </main>
        """,
    )


def item_media_html(item: Any) -> str:
    file_id = int(item["id"])
    target_path = Path(str(item["target_path"]))
    url = f"/file/{file_id}"
    name = html.escape(str(item["stored_filename"]))
    if target_path.suffix.lower().lstrip(".") in {"mp4", "mov", "m4v", "avi", "mpg", "mpeg", "mts", "m2ts", "3gp", "wmv"}:
        return f'<video src="{url}" controls></video>'
    return f'<a href="{url}" target="_blank"><img src="{url}" alt="{name}"></a>'


def nav_link(item: Any | None, label: str, key_nav: str) -> str:
    if item is None:
        return nav_disabled(label)
    return nav_button(f"/item/{int(item['id'])}", label, key_nav)


def month_nav_link(month_key: str | None, label: str, key_nav: str) -> str:
    if month_key is None:
        return nav_disabled(label)
    return nav_button(f"/month/{html.escape(month_key)}", label, key_nav)


def nav_button(href: str, label: str, key_nav: str) -> str:
    return f'<a class="nav-button" href="{href}" data-key-nav="{html.escape(key_nav)}">{html.escape(label)}</a>'


def nav_disabled(label: str) -> str:
    return f'<span class="nav-button disabled">{html.escape(label)}</span>'


def browser_controls_html(
    month_nav: dict[str, str | None],
    previous_item: Any | None,
    next_item: Any | None,
) -> str:
    return f"""
    <nav class="controls" aria-label="Navigering">
      {month_nav_link(month_nav["previous_year"], "Forrige år", "previous-year")}
      {month_nav_link(month_nav["next_year"], "Neste år", "next-year")}
      {month_nav_link(month_nav["previous_month"], "Forrige måned", "previous-month")}
      {month_nav_link(month_nav["next_month"], "Neste måned", "next-month")}
      {nav_link(previous_item, "Forrige bilde", "previous")}
      {nav_link(next_item, "Neste bilde", "next")}
    </nav>
    """


def month_overview_link(month_key: str) -> str:
    if not valid_month_key(month_key):
        return ""
    return f'<a class="month-overview-link" href="/month/{html.escape(month_key)}">Månedsoversikt</a>'


def month_page_html(target: Path, month_key: str, items: list[Any]) -> str:
    cards = "\n".join(month_item_html(target, item) for item in items)
    previous_item = items[-1] if items else None
    next_item = items[0] if items else None
    controls = browser_controls_html(browser_month_navigation_for_key(target, month_key), previous_item, next_item)
    return page_html(
        f"Månedsoversikt: {month_key}",
        f"""
        <main class="server-browser">
          <header class="browser-header">
            <div class="topline">
              <div class="title">Bildebrowser</div>
              <span class="status">{html.escape(month_key)} oversikt ({len(items)} filer)</span>
              <a class="server-search-link" href="/search">Bildesøk</a>
            </div>
            {controls}
          </header>
          <section class="month-grid-server">{cards}</section>
          <footer class="browser-footer">
            <span class="filename">Månedsoversikt: {html.escape(month_key)}</span>
          </footer>
        </main>
        """,
    )


def month_item_html(target: Path, item: Any) -> str:
    target_path = Path(str(item["target_path"]))
    label = html.escape(display_relative_path(target, target_path))
    media = thumbnail_media_html(item)
    return f"""
    <article class="item">
      <a class="thumb-link" href="/item/{int(item["id"])}">{media}</a>
      <div class="text">
        <div class="path">{label}</div>
        <div class="score">{html.escape(format_bytes(int(item["size_bytes"])))}</div>
      </div>
    </article>
    """


def thumbnail_media_html(item: Any) -> str:
    file_id = int(item["id"])
    target_path = Path(str(item["target_path"]))
    url = f"/file/{file_id}"
    name = html.escape(str(item["stored_filename"]))
    if target_path.suffix.lower().lstrip(".") in {"mp4", "mov", "m4v", "avi", "mpg", "mpeg", "mts", "m2ts", "3gp", "wmv"}:
        return f'<div class="video-thumb">Video<br>{name}</div>'
    return f'<img src="{url}" alt="{name}" loading="lazy">'


def page_html(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #171717;
      --panel: #242424;
      --stage: #0e0e0e;
      --border: #3a3a3a;
      --text: #f2f2f2;
      --muted: #b8b8b8;
      --accent: #7db7ff;
      --danger: #ff8a80;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    .shell {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; }}
    .meta {{ color: var(--muted); margin: 0 0 18px; }}
    .search {{ display: grid; grid-template-columns: minmax(0, 1fr) 90px auto; gap: 8px; margin: 18px 0; }}
    input, button {{
      font: inherit;
      padding: 10px 12px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: #303030;
      color: var(--text);
    }}
    button {{ cursor: pointer; }}
    button:hover {{ background: #3a3a3a; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 14px; }}
    .server-browser {{ min-height: 100vh; display: grid; grid-template-rows: auto minmax(0, 1fr) auto; }}
    .browser-header {{
      background: var(--panel);
      border-bottom: 1px solid var(--border);
      padding: 12px;
      display: grid;
      gap: 10px;
    }}
    .topline, .controls {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
    .title {{ font-weight: 700; margin-right: 12px; }}
    .status {{ color: var(--muted); font-size: 14px; }}
    a, .disabled {{ color: var(--accent); }}
    a {{ text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .nav-button, .server-search-link, .month-overview-link {{
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 8px 10px;
      background: #303030;
      color: var(--text);
      min-height: 38px;
      display: inline-flex;
      align-items: center;
    }}
    .nav-button:hover, .server-search-link:hover, .month-overview-link:hover {{ background: #3a3a3a; text-decoration: none; }}
    .disabled {{ color: #777; cursor: default; }}
    .stage {{
      min-height: 0;
      display: grid;
      place-items: center;
      background: var(--stage);
      border-top: 1px solid var(--border);
      overflow: hidden;
      padding: 10px;
    }}
    .stage img, .stage video {{
      max-width: 100%;
      max-height: calc(100vh - 98px);
      object-fit: contain;
      display: block;
    }}
    .month-grid-server {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
      gap: 14px;
      align-content: start;
      padding: 12px;
      overflow: auto;
    }}
    .thumb-link {{ display: block; color: inherit; text-decoration: none; }}
    .thumb-link img, .video-thumb {{
      width: 100%;
      aspect-ratio: 4 / 3;
      object-fit: cover;
      display: grid;
      place-items: center;
      background: #181818;
      text-align: center;
    }}
    .item {{ background: var(--panel); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }}
    .item img {{ width: 100%; aspect-ratio: 4 / 3; object-fit: cover; display: block; background: #181818; }}
    .text {{ padding: 10px; font-size: 14px; }}
    .path {{ overflow-wrap: anywhere; }}
    .score {{ color: var(--muted); margin-top: 4px; }}
    .error {{ color: var(--danger); }}
    .message {{ color: var(--muted); }}
    .browser-footer {{
      background: var(--panel);
      border-top: 1px solid var(--border);
      padding: 8px 12px;
      font-size: 13px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px 14px;
      align-items: center;
      min-width: 0;
    }}
    .filename {{
      min-width: 0;
      overflow: hidden;
      white-space: nowrap;
      text-overflow: ellipsis;
      color: var(--muted);
    }}
    @media (max-width: 640px) {{
      .shell {{ padding: 16px; }}
      .search {{ grid-template-columns: 1fr; }}
      .browser-header {{ align-items: stretch; }}
      .nav-button, .server-search-link, .month-overview-link {{ flex: 1 1 auto; justify-content: center; text-align: center; }}
    }}
  </style>
</head>
<body>
{body}
<script>
  document.addEventListener("keydown", event => {{
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const target = event.target;
    if (
      target instanceof HTMLInputElement ||
      target instanceof HTMLTextAreaElement ||
      target instanceof HTMLSelectElement ||
      target instanceof HTMLButtonElement ||
      target?.isContentEditable
    ) return;
    const selector = {{
      ArrowLeft: '[data-key-nav="previous"]',
      ArrowRight: '[data-key-nav="next"]',
      ArrowUp: '[data-key-nav="previous-month"]',
      ArrowDown: '[data-key-nav="next-month"]',
      PageUp: '[data-key-nav="previous-year"]',
      PageDown: '[data-key-nav="next-year"]',
    }}[event.key] || "";
    if (!selector) return;
    const link = document.querySelector(selector);
    if (!(link instanceof HTMLAnchorElement)) return;
    event.preventDefault();
    window.location.href = link.href;
  }});
</script>
</body>
</html>
"""


def run_server(
    target: Path,
    config: OpenClipConfig,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    ready: Callable[[str], None] | None = None,
) -> None:
    server = BildebankServer((host, port), target, config)
    actual_host, actual_port = server.server_address
    url = f"http://{actual_host}:{actual_port}/"
    if ready is not None:
        ready(url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
