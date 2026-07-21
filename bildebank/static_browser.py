from __future__ import annotations

from pathlib import Path
from typing import Any

from . import db
from .browser_dates import (
    browser_date_from_item,
    browser_date_text,
    item_value,
    month_key_from_browser_date_value,
    month_key_from_path,
)
from .formatting import format_bytes
from .html_paths import path_to_url
from .media import media_kind
from .thumbnails import existing_thumbnail_url
from .video_previews import existing_video_preview_path


def static_browser_item(
    item: Any,
    relative_path: Path,
    *,
    target: Path | None = None,
    url: str | None = None,
    thumbnail_src: str | None = None,
    playback_url: str | None = None,
    kind: str | None = None,
    name: str | None = None,
    month_key: str | None = None,
    browser_date: str | None = None,
    view_rotation: object | None = None,
) -> dict[str, object]:
    kind = kind or media_kind(relative_path)
    url = url if url is not None else path_to_url(relative_path)
    browser_date = browser_date if browser_date is not None else browser_date_from_item(item)
    month_key = month_key or month_key_from_browser_date_value(browser_date) or month_key_from_path(relative_path)
    thumbnail_src = browser_thumbnail_src(target, relative_path, kind, url, thumbnail_src)
    playback_url = browser_playback_url(target, item, relative_path, kind, url, playback_url)
    view_rotation = view_rotation if view_rotation is not None else item_view_rotation(item)
    return {
        "fileId": item_int(item, "id", item_int(item, "fileId", 0)),
        "path": relative_path.as_posix(),
        "url": url,
        "playbackUrl": playback_url or "",
        "originalUrl": url if kind == "video" else "",
        "thumbnailSrc": thumbnail_src,
        "kind": kind,
        "viewRotation": db.normalize_view_rotation(view_rotation),
        "monthKey": month_key,
        "browserDate": browser_date,
        "dateText": static_browser_date_text(item),
        "takenDate": item_value(item, "taken_date", "") or "",
        "dateSource": item_value(item, "date_source", "") or "",
        "manualDateFrom": item_value(item, "manual_date_from", "") or "",
        "manualDateTo": item_value(item, "manual_date_to", "") or "",
        "manualDateNote": item_value(item, "manual_date_note", "") or "",
        "comment": item_value(item, "comment", None),
        "name": name or str(item_value(item, "stored_filename", relative_path.name)),
        "sizeText": item_size_text(item),
    }


def browser_playback_url(
    target: Path | None,
    item: Any,
    relative_path: Path,
    kind: str,
    original_url: str,
    playback_url: str | None,
) -> str | None:
    if playback_url is not None:
        return playback_url
    if kind != "video":
        return None
    if relative_path.suffix.casefold() != ".avi":
        return original_url
    if target is None:
        return None
    preview_path = existing_video_preview_path(target, item)
    if preview_path is None:
        return None
    return path_to_url(preview_path.relative_to(target))


def browser_thumbnail_src(
    target: Path | None,
    relative_path: Path,
    kind: str,
    url: str,
    thumbnail_src: str | None,
) -> str:
    if thumbnail_src is not None:
        return thumbnail_src
    if kind != "image":
        return ""
    if target is None:
        return url
    return existing_thumbnail_url(target, relative_path)


def item_view_rotation(item: Any) -> object:
    value = item_value(item, "view_rotation_degrees", None)
    if value is not None:
        return value
    return item_value(item, "viewRotation", 0)


def static_browser_date_text(item: Any) -> str:
    has_date_fields = any(
        item_value(item, key, "") for key in ("manual_date_from", "manual_date_to", "taken_date", "date_source")
    )
    return browser_date_text(item) if has_date_fields else ""


def item_size_text(item: Any) -> str:
    size_text = item_value(item, "sizeText", None)
    if size_text:
        return str(size_text)
    try:
        return format_bytes(item_int(item, "size_bytes", 0))
    except (TypeError, ValueError):
        return ""


def item_int(item: Any, key: str, default: int) -> int:
    value = item_value(item, key, default) or default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, (str, bytes, bytearray)):
        return int(value)
    return int(str(value))
