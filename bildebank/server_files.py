from __future__ import annotations

import mimetypes
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from . import db
from .server_browser import browser_item_by_id


@dataclass(frozen=True)
class ServerFile:
    content: bytes
    content_type: str


def read_server_file(target: Path, encoded_relative_path: str) -> ServerFile:
    path = server_file_path(target, encoded_relative_path)
    if not path.is_file():
        raise FileNotFoundError("Filen finnes ikke.")
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    content = path.read_bytes()
    return ServerFile(content=content, content_type=content_type)


def server_file_path(target: Path, encoded_relative_path: str) -> Path:
    raw_path = urllib.parse.unquote(encoded_relative_path).strip("/")
    if raw_path.isdigit():
        row = browser_item_by_id(target, int(raw_path))
        if row is None:
            raise FileNotFoundError("Filen finnes ikke.")
        return db.absolute_target_path(target, Path(str(row["target_path"])))

    relative = Path(raw_path)
    path = (target / relative).resolve()
    try:
        path.relative_to(target.resolve())
    except ValueError as exc:
        raise PermissionError("Ugyldig filsti.") from exc
    return path
