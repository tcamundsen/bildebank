from __future__ import annotations

import html
import re
import urllib.parse
from pathlib import Path
from typing import Callable


ShellPageRenderer = Callable[..., str]


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

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                flush_code()
                in_code = False
            else:
                flush_paragraph()
                flush_list()
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not stripped:
            flush_paragraph()
            flush_list()
            continue
        if stripped.startswith("#"):
            flush_paragraph()
            flush_list()
            level = min(len(stripped) - len(stripped.lstrip("#")), 3)
            text = stripped[level:].strip()
            if text:
                html_lines.append(f"<h{level}>{markdown_inline_html(text)}</h{level}>")
            continue
        if stripped.startswith(("- ", "* ")):
            flush_paragraph()
            if list_tag != "ul":
                flush_list()
                list_tag = "ul"
            list_items.append(markdown_inline_html(stripped[2:].strip()))
            continue
        ordered_match = re.match(r"\d+\.\s+(.*)", stripped)
        if ordered_match:
            flush_paragraph()
            if list_tag != "ol":
                flush_list()
                list_tag = "ol"
            list_items.append(markdown_inline_html(ordered_match.group(1).strip()))
            continue
        if list_items:
            list_items[-1] = f"{list_items[-1]} {markdown_inline_html(stripped)}"
            continue
        paragraph.append(stripped)

    flush_paragraph()
    flush_list()
    if in_code:
        flush_code()
    return "\n".join(html_lines)


def strip_markdown_cli_help_markers(markdown: str) -> str:
    lines: list[str] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped in {"<!-- CLI-HELP-START -->", "<!-- CLI-HELP-END -->"}:
            continue
        lines.append(line)
    return "\n".join(lines)


def markdown_inline_html(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"`([^`]+)`", lambda match: f"<code>{match.group(1)}</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", lambda match: f"<strong>{match.group(1)}</strong>", escaped)
    return re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        lambda match: markdown_link_html(match.group(1), match.group(2)),
        escaped,
    )


def markdown_link_html(label: str, url: str) -> str:
    if not safe_markdown_link(url):
        return html.escape(label)
    return f'<a href="{html.escape(url, quote=True)}">{label}</a>'


def safe_markdown_link(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme in {"", "http", "https"} and not url.startswith("//")
