#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

DOCS_DIR = Path("docs")
OUT_DIR = Path("html")
SCRIPT_FILE = Path(__file__)
DOC_PATTERNS = ("*.md", "web/*.md")

HTML_TEMPLATE = """\
<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <title>$title$</title>
  <style>
    body {
      max-width: 850px;
      margin: 3rem auto;
      padding: 0 1rem;
      font-family: system-ui, sans-serif;
      line-height: 1.55;
    }

    code {
      background: #eee;
      padding: 0.1rem 0.25rem;
      border-radius: 0.2rem;
    }

    pre {
      background: #f5f5f5;
      padding: 1rem;
      overflow-x: auto;
    }
  </style>
</head>
<body>
$body$
</body>
</html>
"""


def rewrite_md_links(html_file: Path) -> None:
    text = html_file.read_text(encoding="utf-8")

    text = re.sub(
        r'href="([^"#?]+)\.md([#?][^"]*)?"',
        lambda m: f'href="{m.group(1)}.html{m.group(2) or ""}"',
        text,
    )

    html_file.write_text(text, encoding="utf-8")


def needs_generation(md_file: Path, out_file: Path) -> bool:
    if not out_file.exists():
        return True

    out_mtime = out_file.stat().st_mtime
    return md_file.stat().st_mtime > out_mtime or SCRIPT_FILE.stat().st_mtime > out_mtime


def find_markdown_files() -> list[Path]:
    files: list[Path] = []
    for pattern in DOC_PATTERNS:
        files.extend(DOCS_DIR.glob(pattern))
    return sorted(files)


def output_path_for(md_file: Path) -> Path:
    relative_md_file = md_file.relative_to(DOCS_DIR)
    return OUT_DIR / relative_md_file.with_suffix(".html")


def write_template_file() -> Path:
    template = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".html",
        delete=False,
    )
    try:
        template.write(HTML_TEMPLATE)
        return Path(template.name)
    finally:
        template.close()


def render_markdown(md_file: Path, out_file: Path) -> None:
    if not md_file.exists():
        print(f"Fant ikke Markdown-fil: {md_file}", file=sys.stderr)
        sys.exit(1)

    out_file.parent.mkdir(parents=True, exist_ok=True)
    template_file = write_template_file()

    try:
        subprocess.run(
            [
                "pandoc",
                str(md_file),
                "-f",
                "markdown",
                "-t",
                "html5",
                "--standalone",
                "--template",
                str(template_file),
                "--metadata",
                f"title={md_file.stem}",
                "-o",
                str(out_file),
            ],
            check=True,
        )
    finally:
        template_file.unlink(missing_ok=True)

    rewrite_md_links(out_file)
    print(f"Laget {out_file}")


def render_all_changed_files() -> None:
    for md_file in find_markdown_files():
        out_file = output_path_for(md_file)
        if needs_generation(md_file, out_file):
            render_markdown(md_file, out_file)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", nargs="?", help="Markdown-fil som skal konverteres")
    parser.add_argument("output", nargs="?", help="HTML-fil som skal skrives")

    args = parser.parse_args()

    if args.source is None and args.output is None:
        render_all_changed_files()
        return

    if args.source is None or args.output is None:
        parser.error("oppgi enten både source og output, eller ingen av dem")

    render_markdown(Path(args.source), Path(args.output))


if __name__ == "__main__":
    main()
