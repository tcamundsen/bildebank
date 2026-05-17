#!/bin/python3

from pathlib import Path
import subprocess
import re

DOCS_DIR = Path("docs")
OUT_DIR = Path("html")
SCRIPT_FILE = Path(__file__)


HTML_TEMPLATE = """
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


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)

    template_file = OUT_DIR / "_template.html"
    template_file.write_text(HTML_TEMPLATE, encoding="utf-8")

    for md_file in sorted(DOCS_DIR.glob("*.md")):
        out_file = OUT_DIR / f"{md_file.stem}.html"
        if not needs_generation(md_file, out_file):
            #print(f"Hopper over {out_file}")
            continue

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
        rewrite_md_links(out_file)

        print(f"Laget {out_file}")

    template_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
