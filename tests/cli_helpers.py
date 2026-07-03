from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from bildebank.cli import main


def run_cli(args: list[str]) -> int:
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        return main(args)


def capture_cli(args: list[str]) -> tuple[int, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(args)
    return code, stdout.getvalue(), stderr.getvalue()


def write_fake_exiftool(path: Path, body: str | None = None) -> None:
    script_body = body or "import json\nprint(json.dumps([]))\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""#!/usr/bin/env python3
import sys
if "-ver" in sys.argv:
    print("13.58")
    raise SystemExit(0)
{script_body}
""",
        encoding="utf-8",
        newline="\n",
    )
    path.chmod(0o755)


def write_test_image(path: Path, *, size: tuple[int, int] = (8, 8), color: tuple[int, int, int] = (200, 20, 20)) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, color)
    image.save(path)
