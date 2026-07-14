from __future__ import annotations

import urllib.parse
from http import HTTPStatus
from typing import TYPE_CHECKING

from . import server_app, server_geo, server_request
from .config import BrowserHotkeyConfig
from .file_tags import create_user_tag, delete_user_tag, rename_user_tag
from .server_faces import clear_face_caches
from .server_geo import DEFAULT_GEO_LIMIT
from .server_pages import error_html
from .server_request import first_param, positive_int_param
from .server_search import OpenClipSearchCache
from .target_lock import TargetLockError

if TYPE_CHECKING:
    from .server_handler import BildebankRequestHandler


def settings_redirect_location(params: dict[str, list[str]]) -> str:
    raw_scroll_y = first_param(params, "scroll_y").strip()
    if not raw_scroll_y:
        return "/settings"
    try:
        scroll_y = int(raw_scroll_y)
    except ValueError:
        return "/settings"
    if scroll_y <= 0:
        return "/settings"
    return f"/settings?scroll={scroll_y}"


def respond_set_geo_place_name(handler: BildebankRequestHandler) -> None:
    params = server_request.read_form_params(handler.headers, handler.rfile)
    h3_cell = first_param(params, "h3_cell").strip()
    name = first_param(params, "name")
    limit = positive_int_param(params, "limit", DEFAULT_GEO_LIMIT)
    try:
        server_geo.set_geo_place_name(handler.server.target, h3_cell, name)
    except TargetLockError as exc:
        handler.respond_text(str(exc), status=HTTPStatus.CONFLICT)
        return
    except ValueError as exc:
        handler.respond_text(str(exc), status=HTTPStatus.BAD_REQUEST)
        return
    url = "/geo/area/" + urllib.parse.quote(h3_cell, safe="") + f"?limit={limit}"
    handler.redirect(url)


def respond_set_custom_geo_place(handler: BildebankRequestHandler) -> None:
    params = server_request.read_form_params(handler.headers, handler.rfile)
    try:
        server_geo.save_custom_geo_place(
            handler.server.target,
            raw_original_slug=first_param(params, "original_slug"),
            raw_slug=first_param(params, "slug"),
            name=first_param(params, "name"),
            raw_h3_cells=first_param(params, "h3_cells"),
        )
    except TargetLockError as exc:
        handler.respond_html(
            error_html(
                exc,
                face_enabled=handler.server.face_enabled,
                openclip_enabled=handler.server.openclip_enabled,
            ),
            status=HTTPStatus.CONFLICT,
        )
        return
    except ValueError as exc:
        handler.respond_html(
            error_html(
                exc,
                face_enabled=handler.server.face_enabled,
                openclip_enabled=handler.server.openclip_enabled,
            ),
            status=HTTPStatus.BAD_REQUEST,
        )
        return
    handler.redirect("/geo/custom-places")


def respond_delete_custom_geo_place(handler: BildebankRequestHandler) -> None:
    params = server_request.read_form_params(handler.headers, handler.rfile)
    try:
        server_geo.delete_custom_geo_place(
            handler.server.target,
            first_param(params, "original_slug") or first_param(params, "slug"),
        )
    except TargetLockError as exc:
        handler.respond_html(
            error_html(
                exc,
                face_enabled=handler.server.face_enabled,
                openclip_enabled=handler.server.openclip_enabled,
            ),
            status=HTTPStatus.CONFLICT,
        )
        return
    except ValueError as exc:
        handler.respond_html(
            error_html(
                exc,
                face_enabled=handler.server.face_enabled,
                openclip_enabled=handler.server.openclip_enabled,
            ),
            status=HTTPStatus.BAD_REQUEST,
        )
        return
    handler.redirect("/geo")


def respond_set_face_config(handler: BildebankRequestHandler) -> None:
    params = server_request.read_form_params(handler.headers, handler.rfile)
    enabled = "true" in {value.strip().lower() for value in params.get("enabled", [])}
    handler.server.config = server_app.update_face_enabled_config(
        handler.server.config,
        server_app.server_program_repo_root(),
        enabled,
    )
    handler.redirect(settings_redirect_location(params))


def respond_set_image_search(handler: BildebankRequestHandler) -> None:
    params = server_request.read_form_params(handler.headers, handler.rfile)
    enabled = "true" in {value.strip().lower() for value in params.get("enabled", [])}
    handler.server.config = server_app.update_image_search_enabled_config(
        handler.server.config,
        server_app.server_program_repo_root(),
        enabled,
    )
    handler.server.search_cache = OpenClipSearchCache(handler.server.config)
    handler.redirect(settings_redirect_location(params))


def respond_set_hide_out_of_focus(handler: BildebankRequestHandler) -> None:
    params = server_request.read_form_params(handler.headers, handler.rfile)
    enabled = "true" in {value.strip().lower() for value in params.get("enabled", [])}
    handler.server.config = server_app.update_hide_out_of_focus_config(
        handler.server.config,
        server_app.server_program_repo_root(),
        enabled,
    )
    clear_cache = getattr(handler.server, "clear_browser_navigation_cache", None)
    if clear_cache is not None:
        clear_cache()
    handler.redirect(settings_redirect_location(params))


def respond_set_manual_person_controls(handler: BildebankRequestHandler) -> None:
    params = server_request.read_form_params(handler.headers, handler.rfile)
    enabled = "true" in {value.strip().lower() for value in params.get("enabled", [])}
    handler.server.config = server_app.update_manual_person_controls_config(
        handler.server.config,
        server_app.server_program_repo_root(),
        enabled,
    )
    handler.redirect(settings_redirect_location(params))


def respond_set_person_reference_links(handler: BildebankRequestHandler) -> None:
    params = server_request.read_form_params(handler.headers, handler.rfile)
    enabled = "true" in {value.strip().lower() for value in params.get("enabled", [])}
    handler.server.config = server_app.update_person_reference_links_config(
        handler.server.config,
        server_app.server_program_repo_root(),
        enabled,
    )
    clear_face_caches()
    handler.redirect(settings_redirect_location(params))


def respond_set_hotkey(handler: BildebankRequestHandler) -> None:
    params = server_request.read_form_params(handler.headers, handler.rfile)
    key = first_param(params, "key").strip()
    action = first_param(params, "action").strip()
    if action == "h3":
        hotkey = BrowserHotkeyConfig(action=action, h3_cell=first_param(params, "h3_cell").strip())
    elif action == "person":
        hotkey = BrowserHotkeyConfig(action=action, person_name=first_param(params, "person_name").strip())
    elif action == "tag":
        hotkey = BrowserHotkeyConfig(action=action, tag_name=first_param(params, "tag_name").strip())
    elif action == "manual_date":
        hotkey = BrowserHotkeyConfig(
            action=action,
            mode=first_param(params, "mode").strip(),
            date=first_param(params, "date").strip(),
            uncertainty=first_param(params, "uncertainty").strip(),
            date_from=first_param(params, "date_from").strip(),
            date_to=first_param(params, "date_to").strip(),
            note=first_param(params, "note").strip(),
        )
    else:
        hotkey = BrowserHotkeyConfig(action=action)
    try:
        handler.server.config = server_app.update_hotkey_config(
            handler.server.config,
            server_app.server_program_repo_root(),
            key,
            hotkey,
        )
    except ValueError as exc:
        handler.respond_text(str(exc), status=HTTPStatus.BAD_REQUEST)
        return
    handler.redirect(settings_redirect_location(params))


def respond_set_hotkey_hints(handler: BildebankRequestHandler) -> None:
    params = server_request.read_form_params(handler.headers, handler.rfile)
    enabled = "true" in {value.strip().lower() for value in params.get("enabled", [])}
    handler.server.config = server_app.update_hotkey_hints_config(
        handler.server.config,
        server_app.server_program_repo_root(),
        enabled,
    )
    handler.redirect(settings_redirect_location(params))


def respond_set_h3_cell_name(handler: BildebankRequestHandler) -> None:
    params = server_request.read_form_params(handler.headers, handler.rfile)
    try:
        server_app.save_h3_cell_name(
            handler.server.target,
            original_h3_cell=first_param(params, "original_h3_cell"),
            h3_cell=first_param(params, "h3_cell"),
            name=first_param(params, "name"),
        )
    except TargetLockError as exc:
        handler.respond_html(
            error_html(
                exc,
                face_enabled=handler.server.face_enabled,
                openclip_enabled=handler.server.openclip_enabled,
            ),
            status=HTTPStatus.CONFLICT,
        )
        return
    except ValueError as exc:
        handler.respond_html(
            error_html(
                exc,
                face_enabled=handler.server.face_enabled,
                openclip_enabled=handler.server.openclip_enabled,
            ),
            status=HTTPStatus.BAD_REQUEST,
        )
        return
    handler.redirect("/settings/h3-cells")


def respond_delete_h3_cell_name(handler: BildebankRequestHandler) -> None:
    params = server_request.read_form_params(handler.headers, handler.rfile)
    try:
        server_app.delete_h3_cell_name(
            handler.server.target,
            h3_cell=first_param(params, "original_h3_cell") or first_param(params, "h3_cell"),
        )
    except TargetLockError as exc:
        handler.respond_html(
            error_html(
                exc,
                face_enabled=handler.server.face_enabled,
                openclip_enabled=handler.server.openclip_enabled,
            ),
            status=HTTPStatus.CONFLICT,
        )
        return
    except ValueError as exc:
        handler.respond_html(
            error_html(
                exc,
                face_enabled=handler.server.face_enabled,
                openclip_enabled=handler.server.openclip_enabled,
            ),
            status=HTTPStatus.BAD_REQUEST,
        )
        return
    handler.redirect("/settings/h3-cells")


def respond_set_face_model(handler: BildebankRequestHandler) -> None:
    params = server_request.read_form_params(handler.headers, handler.rfile)
    model_name = (params.get("model_name") or [""])[0].strip()
    handler.server.config = server_app.update_face_model_config(
        handler.server.config,
        server_app.server_program_repo_root(),
        model_name,
    )
    handler.redirect(settings_redirect_location(params))


def respond_create_tag(handler: BildebankRequestHandler) -> None:
    params = server_request.read_form_params(handler.headers, handler.rfile)
    try:
        create_user_tag(handler.server.target, first_param(params, "name"))
    except TargetLockError as exc:
        handler.respond_html(
            error_html(
                exc,
                face_enabled=handler.server.face_enabled,
                openclip_enabled=handler.server.openclip_enabled,
            ),
            status=HTTPStatus.CONFLICT,
        )
        return
    except ValueError as exc:
        handler.respond_html(
            error_html(
                exc,
                face_enabled=handler.server.face_enabled,
                openclip_enabled=handler.server.openclip_enabled,
            ),
            status=HTTPStatus.BAD_REQUEST,
        )
        return
    handler.redirect("/tags")


def respond_rename_tag(handler: BildebankRequestHandler) -> None:
    params = server_request.read_form_params(handler.headers, handler.rfile)
    try:
        tag_id = int(first_param(params, "tag_id"))
    except ValueError:
        handler.respond_text("Ugyldig tagg-id.", status=HTTPStatus.BAD_REQUEST)
        return
    try:
        rename_user_tag(handler.server.target, tag_id=tag_id, new_name=first_param(params, "name"))
    except TargetLockError as exc:
        handler.respond_html(
            error_html(
                exc,
                face_enabled=handler.server.face_enabled,
                openclip_enabled=handler.server.openclip_enabled,
            ),
            status=HTTPStatus.CONFLICT,
        )
        return
    except ValueError as exc:
        handler.respond_html(
            error_html(
                exc,
                face_enabled=handler.server.face_enabled,
                openclip_enabled=handler.server.openclip_enabled,
            ),
            status=HTTPStatus.BAD_REQUEST,
        )
        return
    handler.redirect("/tags")


def respond_delete_tag(handler: BildebankRequestHandler) -> None:
    params = server_request.read_form_params(handler.headers, handler.rfile)
    try:
        tag_id = int(first_param(params, "tag_id"))
    except ValueError:
        handler.respond_text("Ugyldig tagg-id.", status=HTTPStatus.BAD_REQUEST)
        return
    try:
        delete_user_tag(handler.server.target, tag_id=tag_id)
    except TargetLockError as exc:
        handler.respond_html(
            error_html(
                exc,
                face_enabled=handler.server.face_enabled,
                openclip_enabled=handler.server.openclip_enabled,
            ),
            status=HTTPStatus.CONFLICT,
        )
        return
    except ValueError as exc:
        handler.respond_html(
            error_html(
                exc,
                face_enabled=handler.server.face_enabled,
                openclip_enabled=handler.server.openclip_enabled,
            ),
            status=HTTPStatus.BAD_REQUEST,
        )
        return
    handler.redirect("/tags")
