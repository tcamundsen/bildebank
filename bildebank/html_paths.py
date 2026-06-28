from __future__ import annotations

from pathlib import Path
from urllib.parse import quote


def relative_to_target(target: Path, path: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        return candidate
    try:
        return candidate.resolve().relative_to(target.resolve())
    except ValueError:
        return candidate


def display_relative_path(target: Path, path: Path) -> str:
    return relative_to_target(target, path).as_posix()


def path_to_url(path: Path) -> str:
    path_text = str(path).replace("\\", "/")
    return "/".join(quote(part) for part in path_text.split("/"))
