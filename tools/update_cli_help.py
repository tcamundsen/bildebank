from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

START_MARKER = "<!-- CLI-HELP-START -->"
END_MARKER = "<!-- CLI-HELP-END -->"
DOCS_DIR = Path("docs")
DOC_PATTERNS = ("*.md", "web/*.md")


def get_help_output(command_name: str) -> str:
    result = subprocess.run(
        ["bildebank", command_name, "--help"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.rstrip()


def build_help_block(help_output: str) -> str:
    return (
        f"{START_MARKER}\n"
        "```text\n"
        f"{help_output}\n"
        "```\n"
        f"{END_MARKER}"
    )


def find_markdown_files() -> list[Path]:
    files: list[Path] = []
    for pattern in DOC_PATTERNS:
        files.extend(DOCS_DIR.glob(pattern))
    return sorted(files)


def docs_path_from_argument(value: str) -> Path:
    path = Path(value)
    if path.suffix == ".md" or path.parent != Path("."):
        return path

    return DOCS_DIR / f"{value}.md"


def command_name_from_docs_path(docs_path: Path) -> str:
    return docs_path.stem


def marker_positions(content: str) -> tuple[int, int] | None:
    start = content.find(START_MARKER)
    end = content.find(END_MARKER)

    if start == -1 and end == -1:
        return None

    if start == -1 or end == -1 or end < start:
        raise ValueError("manglende eller ugyldige markører")

    return start, end + len(END_MARKER)


def update_markdown_file(docs_path: Path) -> bool:
    if not docs_path.exists():
        print(f"Fant ikke dokumentasjon: {docs_path}", file=sys.stderr)
        sys.exit(1)

    content = docs_path.read_text(encoding="utf-8")

    try:
        positions = marker_positions(content)
    except ValueError as exc:
        print(f"{exc} i {docs_path}", file=sys.stderr)
        sys.exit(1)

    if positions is None:
        print(f"Ingen CLI-help-markører i {docs_path}; hopper over")
        return False

    start, end = positions
    command_name = command_name_from_docs_path(docs_path)
    help_output = get_help_output(command_name)
    new_block = build_help_block(help_output)
    updated = content[:start] + new_block + content[end:]

    if updated == content:
        print(f"CLI-help er allerede oppdatert i {docs_path}")
        return False

    docs_path.write_text(updated, encoding="utf-8")
    print(f"Oppdaterte CLI-help i {docs_path}")
    return True


def update_all_markdown_files() -> None:
    for docs_path in find_markdown_files():
        update_markdown_file(docs_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "target",
        nargs="?",
        help="For eksempel docs/face-scan.md eller face-scan. Uten argument oppdateres alle relevante .md-filer.",
    )

    args = parser.parse_args()

    if args.target is None:
        update_all_markdown_files()
        return

    update_markdown_file(docs_path_from_argument(args.target))


if __name__ == "__main__":
    main()
