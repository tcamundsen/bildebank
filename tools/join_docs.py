#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DOC_PATTERNS = ("*.md", "web/*.md")
CLI_HELP_MARKER_RE = re.compile(r"^<!-- CLI-HELP-(START|END) -->$")


def find_default_docs_dir() -> Path:
    cwd_docs = Path("docs")
    if cwd_docs.is_dir():
        return cwd_docs

    repo_docs = Path(__file__).resolve().parent.parent / "docs"
    if repo_docs.is_dir():
        return repo_docs

    return cwd_docs


def find_markdown_files(
    docs_dir: Path,
    exclude_file: Path | None = None,
) -> list[Path]:
    files: list[Path] = []
    for pattern in DOC_PATTERNS:
        files.extend(docs_dir.glob(pattern))

    resolved_exclude = exclude_file.resolve() if exclude_file and exclude_file.exists() else None

    result: list[Path] = []
    for file in sorted(files):
        if not file.is_file():
            continue
        if resolved_exclude and file.resolve() == resolved_exclude:
            continue
        result.append(file)

    return result


def strip_cli_help_markers(text: str) -> str:
    lines = text.splitlines()
    filtered = [line for line in lines if not CLI_HELP_MARKER_RE.match(line.strip())]
    return "\n".join(filtered)


def join_markdown_files(
    files: list[Path],
    base_dir: Path,
    include_markers: bool = True,
) -> str:
    chunks: list[str] = []

    for file in files:
        raw_content = file.read_text(encoding="utf-8")
        content = strip_cli_help_markers(raw_content).strip()
        if not content:
            continue

        try:
            rel_path = file.relative_to(base_dir.parent)
        except ValueError:
            rel_path = file

        if include_markers:
            chunks.append(f"## Fil: {rel_path}\n\n{content}")
        else:
            chunks.append(content)

    if not chunks:
        return ""

    return "\n\n".join(chunks) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Slår sammen alle Markdown-filer i docs og docs/web til én stor Markdown-fil."
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Sti til utdatafil (standard: docs/alle-dokumenter.md)",
    )
    parser.add_argument(
        "--docs-dir",
        type=Path,
        default=None,
        help="Sti til docs-mappen (standard: docs)",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Skriv resultatet til stdout istedenfor fil",
    )
    parser.add_argument(
        "--no-markers",
        action="store_true",
        help="Sløyf '## Fil: ...' fil-markører i samlet dokument",
    )

    args = parser.parse_args()

    docs_dir = args.docs_dir if args.docs_dir is not None else find_default_docs_dir()

    if not docs_dir.exists() or not docs_dir.is_dir():
        print(f"Feil: Finner ikke mappen {docs_dir}", file=sys.stderr)
        sys.exit(1)

    output_path = args.output
    if output_path is None and not args.stdout:
        output_path = docs_dir / "alle-dokumenter.md"

    files = find_markdown_files(docs_dir, exclude_file=output_path)

    if not files:
        print(f"Ingen Markdown-filer funnet i {docs_dir}", file=sys.stderr)
        sys.exit(1)

    joined_text = join_markdown_files(
        files,
        base_dir=docs_dir,
        include_markers=not args.no_markers,
    )

    if args.stdout:
        sys.stdout.write(joined_text)
    else:
        assert output_path is not None
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(joined_text, encoding="utf-8")
        print(
            f"Slo sammen {len(files)} Markdown-filer fra {docs_dir} til {output_path} ({len(joined_text)} tegn)."
        )


if __name__ == "__main__":
    main()
