from __future__ import annotations

import html
import re
import urllib.parse
from pathlib import Path
from typing import Callable


ShellPageRenderer = Callable[..., str]
DOC_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def resolve_doc_path(raw_doc_path: str, docs_root: Path) -> Path | None:
    raw_path = urllib.parse.unquote(raw_doc_path).strip("/")
    if not raw_path:
        return None
    relative = Path(raw_path)
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        return None
    if relative.suffix != ".md":
        relative = relative.with_suffix(".md")
    clean_docs_root = docs_root.resolve()
    candidate = (clean_docs_root / relative).resolve()
    try:
        candidate.relative_to(clean_docs_root)
    except ValueError:
        return None
    return candidate


def resolve_doc_asset_path(raw_asset_path: str, docs_root: Path) -> Path | None:
    raw_path = urllib.parse.unquote(raw_asset_path).strip()
    if not raw_path or "\\" in raw_path:
        return None
    parsed = urllib.parse.urlsplit(raw_path)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None
    if parsed.path.startswith("/") or parsed.path.startswith("//"):
        return None
    relative = Path(parsed.path)
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        return None
    if relative.suffix.casefold() not in DOC_IMAGE_SUFFIXES:
        return None
    clean_docs_root = docs_root.resolve()
    candidate = (clean_docs_root / relative).resolve()
    try:
        candidate.relative_to(clean_docs_root)
    except ValueError:
        return None
    return candidate


def doc_asset_path_has_image_suffix(raw_asset_path: str) -> bool:
    raw_path = urllib.parse.unquote(raw_asset_path).strip()
    parsed = urllib.parse.urlsplit(raw_path)
    return Path(parsed.path).suffix.casefold() in DOC_IMAGE_SUFFIXES


def markdown_doc_page_html(
    doc_path: Path,
    markdown: str,
    *,
    shell_page_html: ShellPageRenderer,
    face_enabled: bool = True,
    openclip_enabled: bool = True,
) -> str:
    title = markdown_doc_title(markdown, doc_path)
    body = markdown_to_html(markdown)
    return shell_page_html(
        title,
        f"""
        <article class="doc-content">
          {body}
        </article>
        """,
        main_class="shell doc-page",
        face_enabled=face_enabled,
        openclip_enabled=openclip_enabled,
    )


def markdown_doc_title(markdown: str, doc_path: Path) -> str:
    for line in markdown.splitlines():
        clean = line.strip()
        if clean.startswith("# "):
            return clean[2:].strip()
    return doc_path.stem.replace("-", " ").title()


def markdown_to_html(markdown: str) -> str:
    lines = strip_markdown_cli_help_markers(markdown).splitlines()
    html_lines: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    list_tag = "ul"
    in_code = False
    code_lines: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            html_lines.append("<p>" + markdown_inline_html(" ".join(paragraph)) + "</p>")
            paragraph.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if list_items:
            html_lines.append(
                f"<{list_tag}>" + "".join(f"<li>{item}</li>" for item in list_items) + f"</{list_tag}>"
            )
            list_items.clear()
            list_tag = "ul"

    def flush_code() -> None:
        if code_lines:
            html_lines.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
            code_lines.clear()

    line_index = 0
    while line_index < len(lines):
        line = lines[line_index]
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                flush_code()
                in_code = False
            else:
                flush_paragraph()
                flush_list()
                in_code = True
            line_index += 1
            continue
        if in_code:
            code_lines.append(line)
            line_index += 1
            continue
        if not stripped:
            flush_paragraph()
            flush_list()
            line_index += 1
            continue
        alert = markdown_alert_html(lines, line_index)
        if alert is not None:
            alert_html, line_index = alert
            flush_paragraph()
            flush_list()
            html_lines.append(alert_html)
            continue
        table = markdown_table_html(lines, line_index)
        if table is not None:
            table_html, line_index = table
            flush_paragraph()
            flush_list()
            html_lines.append(table_html)
            continue
        if stripped.startswith("#"):
            flush_paragraph()
            flush_list()
            level = min(len(stripped) - len(stripped.lstrip("#")), 3)
            text = stripped[level:].strip()
            if text:
                html_lines.append(f"<h{level}>{markdown_inline_html(text)}</h{level}>")
            line_index += 1
            continue
        if stripped.startswith(("- ", "* ")):
            flush_paragraph()
            if list_tag != "ul":
                flush_list()
                list_tag = "ul"
            list_items.append(markdown_inline_html(stripped[2:].strip()))
            line_index += 1
            continue
        ordered_match = re.match(r"\d+\.\s+(.*)", stripped)
        if ordered_match:
            flush_paragraph()
            if list_tag != "ol":
                flush_list()
                list_tag = "ol"
            list_items.append(markdown_inline_html(ordered_match.group(1).strip()))
            line_index += 1
            continue
        if list_items:
            list_items[-1] = f"{list_items[-1]} {markdown_inline_html(stripped)}"
            line_index += 1
            continue
        paragraph.append(stripped)
        line_index += 1

    flush_paragraph()
    flush_list()
    if in_code:
        flush_code()
    return "\n".join(html_lines)


def markdown_alert_html(lines: list[str], start_index: int) -> tuple[str, int] | None:
    marker = re.fullmatch(r">\s*\[!WARNING\]\s*", lines[start_index].strip())
    if marker is None:
        return None

    content_lines: list[str] = []
    index = start_index + 1
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            break
        if not stripped.startswith(">"):
            break
        content_lines.append(stripped[1:].strip())
        index += 1

    body = "<br>".join(markdown_inline_html(line) for line in content_lines if line)
    return (
        '<div class="markdown-alert markdown-alert-warning">'
        '<div class="markdown-alert-title"><span aria-hidden="true">&#9888;</span> Warning</div>'
        f"<p>{body}</p>"
        "</div>",
        index,
    )


def markdown_table_html(lines: list[str], start_index: int) -> tuple[str, int] | None:
    if start_index + 1 >= len(lines):
        return None
    header = markdown_table_row(lines[start_index])
    separator = markdown_table_row(lines[start_index + 1])
    if header is None or separator is None:
        return None
    if len(header) != len(separator) or not all(markdown_table_separator_cell(cell) for cell in separator):
        return None

    rows: list[list[str]] = []
    index = start_index + 2
    while index < len(lines):
        row = markdown_table_row(lines[index])
        if row is None or len(row) != len(header):
            break
        rows.append(row)
        index += 1

    head_html = "".join(f"<th>{markdown_inline_html(cell)}</th>" for cell in header)
    body_html = "".join(
        "<tr>" + "".join(f"<td>{markdown_inline_html(cell)}</td>" for cell in row) + "</tr>" for row in rows
    )
    return f"<table><thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table>", index


def markdown_table_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if "|" not in stripped:
        return None
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    cells = [cell.strip() for cell in stripped.split("|")]
    if len(cells) < 2:
        return None
    return cells


def markdown_table_separator_cell(cell: str) -> bool:
    return re.fullmatch(r":?-{3,}:?", cell.strip()) is not None


def strip_markdown_cli_help_markers(markdown: str) -> str:
    lines: list[str] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped in {"<!-- CLI-HELP-START -->", "<!-- CLI-HELP-END -->"}:
            continue
        lines.append(line)
    return "\n".join(lines)


def markdown_inline_html(text: str) -> str:
    image_replacements: list[str] = []

    def store_image(match: re.Match[str]) -> str:
        index = len(image_replacements)
        image_replacements.append(markdown_image_html(match.group(1), match.group(2)))
        return f"\x00BILDEBANK_IMAGE_{index}\x00"

    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", store_image, text)
    escaped = html.escape(text)
    escaped = re.sub(r"`([^`]+)`", lambda match: f"<code>{match.group(1)}</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", lambda match: f"<strong>{match.group(1)}</strong>", escaped)
    escaped = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda match: markdown_link_html(match.group(1), match.group(2)),
        escaped,
    )
    for index, replacement in enumerate(image_replacements):
        escaped = escaped.replace(f"\x00BILDEBANK_IMAGE_{index}\x00", replacement)
    return escaped


def markdown_image_html(alt: str, url: str) -> str:
    if not safe_markdown_image_url(url):
        return html.escape(alt)
    return (
        f'<img src="{html.escape(url, quote=True)}" '
        f'alt="{html.escape(alt, quote=True)}" loading="lazy">'
    )


def markdown_link_html(label: str, url: str) -> str:
    if not safe_markdown_link(url):
        return html.escape(label)
    return f'<a href="{html.escape(url, quote=True)}">{label}</a>'


def safe_markdown_link(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme in {"", "http", "https"} and not url.startswith("//")


def safe_markdown_image_url(url: str) -> bool:
    if "\\" in url:
        return False
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return False
    if parsed.path.startswith("/") or parsed.path.startswith("//"):
        return False
    path = urllib.parse.unquote(parsed.path)
    relative = Path(path)
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        return False
    return relative.suffix.casefold() in DOC_IMAGE_SUFFIXES
