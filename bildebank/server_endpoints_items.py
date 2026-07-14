from __future__ import annotations

import time
import urllib.parse
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import db, server_actions, server_request
from .config import BrowserHotkeyConfig, HOTKEY_KEYS
from .server_browser_queries import adjacent_items_from_id_order, adjacent_source_items, source_item_by_id
from .server_browser_sources import (
    BrowserSource,
    parse_source_path,
    person_item_url,
    source_has_sql_filter,
    source_item_url,
)
from .server_faces import clear_face_caches
from .server_filter import text_filter_browser_source
from .target_lock import TargetLockError
from .value_parsing import require_int

if TYPE_CHECKING:
    from .server_handler import BildebankRequestHandler


def clear_browser_navigation_cache(server: Any) -> None:
    clear_cache = getattr(server, "clear_browser_navigation_cache", None)
    if clear_cache is not None:
        clear_cache()


def filter_source_from_url(target: Path, source_url: object) -> BrowserSource | None:
    if not isinstance(source_url, str):
        return None
    try:
        parsed = urllib.parse.urlsplit(source_url.strip())
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None
    raw_path = parsed.path
    if not raw_path.startswith("/filter/"):
        return None
    raw_query, page_mode, raw_value = parse_source_path(raw_path.removeprefix("/filter/"))
    if page_mode is not None or raw_value:
        return None
    query = urllib.parse.unquote(raw_query).strip()
    if not query:
        return None
    try:
        source = text_filter_browser_source(query, target)
    except ValueError:
        return None
    return source if source.root_url == raw_path else None


def respond_rotate_item(handler: BildebankRequestHandler) -> None:
    payload = server_request.read_json_payload(handler.headers, handler.rfile)
    try:
        file_id = require_int(payload.get("file_id"), "file_id")
    except ValueError:
        handler.respond_json({"ok": False, "error": "Ugyldig file_id."}, status=HTTPStatus.BAD_REQUEST)
        return
    direction = str(payload.get("direction") or "")
    filter_source = filter_source_from_url(handler.server.target, payload.get("source_url"))
    previous_filter_item = None
    next_filter_item = None
    if filter_source is not None:
        conn = db.connect(handler.server.target)
        try:
            filter_item = source_item_by_id(handler.server.target, filter_source, file_id, conn=conn)
            if filter_item is not None:
                previous_filter_item, next_filter_item = adjacent_source_items(
                    handler.server.target,
                    filter_source,
                    filter_item,
                    conn=conn,
                )
            else:
                filter_source = None
        finally:
            conn.close()
    try:
        rotation = server_actions.rotate_file_view(handler.server.target, file_id, direction)
    except TargetLockError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
        return
    except ValueError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return
    clear_browser_navigation_cache(handler.server)
    result: dict[str, object] = {"ok": True, "file_id": file_id, "rotation": rotation}
    if filter_source is not None:
        conn = db.connect(handler.server.target)
        try:
            filter_item = source_item_by_id(handler.server.target, filter_source, file_id, conn=conn)
        finally:
            conn.close()
        if filter_item is None:
            if next_filter_item is not None:
                result["redirect_url"] = source_item_url(
                    filter_source,
                    require_int(next_filter_item["id"], "neste file_id"),
                )
            elif previous_filter_item is not None:
                result["redirect_url"] = source_item_url(
                    filter_source,
                    require_int(previous_filter_item["id"], "forrige file_id"),
                )
            else:
                result["redirect_url"] = filter_source.root_url
    handler.respond_json(result)


def respond_tag_item(handler: BildebankRequestHandler) -> None:
    def record_timing(name: str, start: float) -> None:
        recorder = getattr(handler, "record_server_timing", None)
        if recorder is not None:
            recorder(name, start)

    start = time.perf_counter()
    payload = server_request.read_json_payload(handler.headers, handler.rfile)
    record_timing("tag_read_payload", start)

    start = time.perf_counter()
    try:
        file_id = require_int(payload.get("file_id"), "file_id")
    except ValueError:
        handler.respond_json({"ok": False, "error": "Ugyldig file_id."}, status=HTTPStatus.BAD_REQUEST)
        return
    tag_name = str(payload.get("tag_name") or "").strip()
    tagged = bool(payload.get("tagged"))
    if not tag_name:
        handler.respond_json({"ok": False, "error": "Taggnavn mangler."}, status=HTTPStatus.BAD_REQUEST)
        return
    record_timing("tag_validate", start)
    try:
        start = time.perf_counter()
        server_actions.set_tag_on_file(handler.server.target, file_id, tag_name, tagged)
        handler.server.note_tag_navigation_change(tag_name)
        record_timing("tag_apply", start)
    except TargetLockError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
        return
    except ValueError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return
    handler.respond_json(
        {
            "ok": True,
            "file_id": file_id,
            "tag_name": db.normalize_tag_name(tag_name),
            "tagged": tagged,
        }
    )


def respond_remove_manual_location_item(handler: BildebankRequestHandler) -> None:
    payload = server_request.read_json_payload(handler.headers, handler.rfile)
    try:
        file_id = require_int(payload.get("file_id"), "file_id")
    except ValueError:
        handler.respond_json({"ok": False, "error": "Ugyldig file_id."}, status=HTTPStatus.BAD_REQUEST)
        return
    try:
        server_actions.remove_manual_h3_location_from_file(handler.server.target, file_id)
    except TargetLockError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
        return
    except ValueError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return
    handler.respond_json({"ok": True, "file_id": file_id, "gps_source": None})


def respond_manual_location_item(handler: BildebankRequestHandler) -> None:
    payload = server_request.read_json_payload(handler.headers, handler.rfile)
    try:
        file_id = require_int(payload.get("file_id"), "file_id")
    except ValueError:
        handler.respond_json({"ok": False, "error": "Ugyldig file_id."}, status=HTTPStatus.BAD_REQUEST)
        return
    h3_cell = str(payload.get("h3_cell") or "").strip()
    filter_source = filter_source_from_url(handler.server.target, payload.get("source_url"))
    previous_filter_item, next_filter_item = filter_adjacent_items_before_change(
        handler,
        filter_source,
        file_id,
    )
    try:
        server_actions.set_manual_h3_location_on_file(handler.server.target, file_id, h3_cell)
    except TargetLockError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
        return
    except ValueError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return
    clear_browser_navigation_cache(handler.server)
    result: dict[str, object] = {
        "ok": True,
        "file_id": file_id,
        "gps_source": "manual-h3",
        "h3_cell": h3_cell,
    }
    redirect_url = filter_redirect_after_change(
        handler,
        filter_source,
        file_id,
        previous_filter_item,
        next_filter_item,
    )
    if redirect_url:
        result["redirect_url"] = redirect_url
    handler.respond_json(result)


def filter_adjacent_items_before_change(
    handler: BildebankRequestHandler,
    filter_source: BrowserSource | None,
    file_id: int,
) -> tuple[Any | None, Any | None]:
    if filter_source is None:
        return None, None
    cached_source_item_order = getattr(handler.server, "source_item_order", None)
    if source_has_sql_filter(filter_source) and cached_source_item_order is not None:
        item_ids, item_positions = cached_source_item_order(filter_source)
        if file_id not in item_positions:
            return None, None
        return adjacent_items_from_id_order(item_ids, file_id, item_positions)
    conn = db.connect(handler.server.target)
    try:
        filter_item = source_item_by_id(handler.server.target, filter_source, file_id, conn=conn)
        if filter_item is None:
            return None, None
        return adjacent_source_items(handler.server.target, filter_source, filter_item, conn=conn)
    finally:
        conn.close()


def filter_redirect_after_change(
    handler: BildebankRequestHandler,
    filter_source: BrowserSource | None,
    file_id: int,
    previous_filter_item: Any | None,
    next_filter_item: Any | None,
) -> str | None:
    if filter_source is None:
        return None
    conn = db.connect(handler.server.target)
    try:
        filter_item = source_item_by_id(handler.server.target, filter_source, file_id, conn=conn)
    finally:
        conn.close()
    if filter_item is not None:
        return None
    if next_filter_item is not None:
        return source_item_url(filter_source, require_int(next_filter_item["id"], "neste file_id"))
    if previous_filter_item is not None:
        return source_item_url(filter_source, require_int(previous_filter_item["id"], "forrige file_id"))
    return filter_source.root_url


def respond_manual_date_item(handler: BildebankRequestHandler) -> None:
    payload = server_request.read_json_payload(handler.headers, handler.rfile)
    try:
        file_id = require_int(payload.get("file_id"), "file_id")
    except ValueError:
        handler.respond_json({"ok": False, "error": "Ugyldig file_id."}, status=HTTPStatus.BAD_REQUEST)
        return
    try:
        date_from, date_to = server_actions.set_manual_date_on_file(
            handler.server.target,
            file_id,
            mode=str(payload.get("mode") or ""),
            date=str(payload.get("date") or ""),
            uncertainty=str(payload.get("uncertainty") or ""),
            date_from=str(payload.get("date_from") or ""),
            date_to=str(payload.get("date_to") or ""),
            note=str(payload.get("note") or ""),
        )
    except TargetLockError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
        return
    except ValueError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return
    clear_browser_navigation_cache(handler.server)
    handler.respond_json(
        {
            "ok": True,
            "file_id": file_id,
            "manual_date_from": date_from.isoformat(),
            "manual_date_to": date_to.isoformat(),
        }
    )


def respond_clear_manual_date_item(handler: BildebankRequestHandler) -> None:
    payload = server_request.read_json_payload(handler.headers, handler.rfile)
    try:
        file_id = require_int(payload.get("file_id"), "file_id")
    except ValueError:
        handler.respond_json({"ok": False, "error": "Ugyldig file_id."}, status=HTTPStatus.BAD_REQUEST)
        return
    try:
        server_actions.clear_manual_date_on_file(handler.server.target, file_id)
    except TargetLockError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
        return
    except ValueError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return
    clear_browser_navigation_cache(handler.server)
    handler.respond_json({"ok": True, "file_id": file_id})


def respond_hotkey_action(handler: BildebankRequestHandler) -> None:
    def record_timing(name: str, start: float) -> None:
        recorder = getattr(handler, "record_server_timing", None)
        if recorder is not None:
            recorder(name, start)

    start = time.perf_counter()
    payload = server_request.read_json_payload(handler.headers, handler.rfile)
    record_timing("hotkey_read_payload", start)

    start = time.perf_counter()
    if not handler.server.config.browser.hotkey_hints_enabled:
        handler.respond_json(
            {"ok": False, "error": "Hurtigtaster er slått av."},
            status=HTTPStatus.FORBIDDEN,
        )
        return
    try:
        file_id = require_int(payload.get("file_id"), "file_id")
    except ValueError:
        handler.respond_json({"ok": False, "error": "Ugyldig file_id."}, status=HTTPStatus.BAD_REQUEST)
        return
    key = str(payload.get("key") or "").strip()
    if key not in HOTKEY_KEYS:
        handler.respond_json({"ok": False, "error": "Ugyldig hurtigtast."}, status=HTTPStatus.BAD_REQUEST)
        return
    hotkey = (handler.server.config.browser.hotkeys or {}).get(key, BrowserHotkeyConfig())
    if hotkey.action == "person" and not handler.server.face_enabled:
        handler.respond_json({"ok": False, "error": "Ansiktsgjenkjenning er av."}, status=HTTPStatus.FORBIDDEN)
        return
    record_timing("hotkey_validate", start)

    start = time.perf_counter()
    filter_source = filter_source_from_url(handler.server.target, payload.get("source_url"))
    record_timing("hotkey_filter_parse", start)

    previous_filter_item = None
    next_filter_item = None
    if filter_source is not None:
        start = time.perf_counter()
        cached_source_item_order = getattr(handler.server, "source_item_order", None)
        if source_has_sql_filter(filter_source) and cached_source_item_order is not None:
            item_ids, item_positions = cached_source_item_order(filter_source)
            if file_id in item_positions:
                previous_filter_item, next_filter_item = adjacent_items_from_id_order(
                    item_ids,
                    file_id,
                    item_positions,
                )
            else:
                filter_source = None
        else:
            conn = db.connect(handler.server.target)
            try:
                filter_item = source_item_by_id(handler.server.target, filter_source, file_id, conn=conn)
                if filter_item is not None:
                    previous_filter_item, next_filter_item = adjacent_source_items(
                        handler.server.target,
                        filter_source,
                        filter_item,
                        conn=conn,
                    )
                else:
                    filter_source = None
            finally:
                conn.close()
        record_timing("hotkey_filter_before", start)
    try:
        start = time.perf_counter()
        result = server_actions.apply_browser_hotkey_to_file(
            handler.server.target,
            file_id,
            hotkey,
            face_config=handler.server.config.face_recognition,
        )
        record_timing("hotkey_apply", start)
    except TargetLockError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
        return
    except ValueError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return
    start = time.perf_counter()
    if hotkey.action == "manual_date":
        clear_browser_navigation_cache(handler.server)
    if hotkey.action == "tag":
        handler.server.note_tag_navigation_change(str(result.get("tag_name") or hotkey.tag_name))
    if hotkey.action == "person":
        clear_face_caches()
        result["person_url"] = person_item_url(str(result["person_name"]), file_id, show_faces=False)
        result["confirmed"] = True
    record_timing("hotkey_post_apply", start)
    if filter_source is not None:
        start = time.perf_counter()
        conn = db.connect(handler.server.target)
        try:
            filter_item = source_item_by_id(handler.server.target, filter_source, file_id, conn=conn)
        finally:
            conn.close()
        record_timing("hotkey_filter_after", start)
        if filter_item is None:
            start = time.perf_counter()
            if next_filter_item is not None:
                result["redirect_url"] = source_item_url(
                    filter_source,
                    require_int(next_filter_item["id"], "neste file_id"),
                )
            elif previous_filter_item is not None:
                result["redirect_url"] = source_item_url(
                    filter_source,
                    require_int(previous_filter_item["id"], "forrige file_id"),
                )
            else:
                result["redirect_url"] = filter_source.root_url
            record_timing("hotkey_redirect", start)
    handler.respond_json({"ok": True, "key": key, **result})


def respond_delete_item(handler: BildebankRequestHandler) -> None:
    payload = server_request.read_json_payload(handler.headers, handler.rfile)
    try:
        file_id = require_int(payload.get("file_id"), "file_id")
    except ValueError:
        handler.respond_json({"ok": False, "error": "Ugyldig file_id."}, status=HTTPStatus.BAD_REQUEST)
        return
    try:
        deleted_path = server_actions.remove_file_from_browser(handler.server.target, file_id)
    except TargetLockError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
        return
    except ValueError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return
    clear_browser_navigation_cache(handler.server)
    handler.respond_json({"ok": True, "file_id": file_id, "deleted_path": deleted_path.as_posix()})


def respond_undelete_item(handler: BildebankRequestHandler) -> None:
    payload = server_request.read_json_payload(handler.headers, handler.rfile)
    try:
        file_id = require_int(payload.get("file_id"), "file_id")
    except ValueError:
        handler.respond_json({"ok": False, "error": "Ugyldig file_id."}, status=HTTPStatus.BAD_REQUEST)
        return
    try:
        restored_path = server_actions.undelete_file_from_browser(handler.server.target, file_id)
    except TargetLockError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
        return
    except ValueError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return
    clear_browser_navigation_cache(handler.server)
    handler.respond_json({"ok": True, "file_id": file_id, "restored_path": restored_path.as_posix()})
