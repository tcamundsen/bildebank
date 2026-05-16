from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


START_MARKER = "<!-- CLI-HELP-START -->"
END_MARKER = "<!-- CLI-HELP-END -->"


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


def update_markdown_file(command_name: str) -> None:
    docs_path = Path("docs") / f"{command_name}.md"

    if not docs_path.exists():
        print(f"Fant ikke dokumentasjon: {docs_path}", file=sys.stderr)
        sys.exit(1)

    content = docs_path.read_text(encoding="utf-8")

    start = content.find(START_MARKER)
    end = content.find(END_MARKER)

    if start == -1 or end == -1:
        print(
            f"Manglende markører i {docs_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    end += len(END_MARKER)

    help_output = get_help_output(command_name)
    new_block = build_help_block(help_output)

    updated = content[:start] + new_block + content[end:]

    docs_path.write_text(updated, encoding="utf-8")

    print(f"Oppdaterte CLI-help i {docs_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", help="For eksempel: face-scan")

    args = parser.parse_args()

    update_markdown_file(args.command)


if __name__ == "__main__":
    main()
