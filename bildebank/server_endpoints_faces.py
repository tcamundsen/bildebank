from __future__ import annotations

import urllib.parse
from dataclasses import replace
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from . import server_app, server_request
from .config import set_face_suggest_threshold, validate_face_suggest_threshold
from .face import (
    add_face_to_person,
    add_person_to_file,
    create_person_and_add_face,
    delete_person,
    remove_face_from_person,
    remove_person_from_file,
    rename_person,
    suggest_faces,
)
from .server_browser_sources import (
    missing_face_suggestions_browser_source,
    parse_person_path,
    parse_person_reference_suggestions_path,
    parse_source_path,
    person_browser_source,
    person_item_url,
    person_reference_suggestions_browser_source,
    person_url,
)
from .server_faces import clear_face_caches, person_by_name, person_item_url_for_face
from .server_pages import error_html, people_page_html, person_not_found_html, person_references_page_html
from .server_request import first_param
from .target_lock import TargetLockError
from .value_parsing import require_int

if TYPE_CHECKING:
    from .server_handler import BildebankRequestHandler


def local_return_url(value: str) -> str | None:
    if not value or "\\" in value or any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return None
    if not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return None
    return value


def face_suggest_summary(stats: Any) -> str:
    return (
        "Ansiktsforslag: "
        f"personer={stats.persons}, ukjente_ansikter={stats.unknown_faces}, "
        f"forslag={stats.suggestions}, threshold={stats.threshold:.3f}"
    )


def respond_face_suggest(handler: BildebankRequestHandler) -> None:
    if not handler.server.face_enabled:
        handler.respond_html(
            error_html(
                ValueError("Ansiktsgjenkjenning er av."),
                face_enabled=False,
                openclip_enabled=handler.server.openclip_enabled,
            ),
            status=HTTPStatus.FORBIDDEN,
        )
        return
    params = server_request.read_form_params(handler.headers, handler.rfile)
    return_url = local_return_url(first_param(params, "return_url"))
    try:
        threshold = validate_face_suggest_threshold(first_param(params, "threshold"))
    except ValueError as exc:
        handler.respond_html(
            error_html(exc, face_enabled=True, openclip_enabled=handler.server.openclip_enabled),
            status=HTTPStatus.BAD_REQUEST,
        )
        return
    set_face_suggest_threshold(server_app.server_program_repo_root(), threshold)
    face_config = replace(handler.server.config.face_recognition, suggest_threshold=threshold)
    handler.server.config = replace(handler.server.config, face_recognition=face_config)
    try:
        stats = suggest_faces(handler.server.target, threshold=threshold, config=face_config)
    except TargetLockError as exc:
        handler.respond_html(
            error_html(exc, face_enabled=True, openclip_enabled=handler.server.openclip_enabled),
            status=HTTPStatus.CONFLICT,
        )
        return
    except ValueError as exc:
        handler.respond_html(
            error_html(exc, face_enabled=True, openclip_enabled=handler.server.openclip_enabled),
            status=HTTPStatus.BAD_REQUEST,
        )
        return
    clear_face_caches()
    handler.server.clear_browser_navigation_cache()
    message = face_suggest_summary(stats)
    if return_url is not None:
        fragment = urllib.parse.urlencode({"face-suggest-status": message})
        handler.redirect(f"{return_url}#{fragment}")
        return
    handler.respond_html(
        people_page_html(
            handler.server.target,
            face_config,
            openclip_enabled=handler.server.openclip_enabled,
            message=message,
            read_only=getattr(handler.server, "read_only", False),
        )
    )


def respond_person(handler: BildebankRequestHandler, raw_path: str) -> None:
    raw_name, person_mode, show_faces, page_mode, raw_value = parse_person_path(raw_path)
    person_name = urllib.parse.unquote(raw_name).strip()
    if not person_name:
        handler.respond_text("Personnavn mangler.", status=HTTPStatus.BAD_REQUEST)
        return
    person = person_by_name(handler.server.target, person_name, handler.server.config.face_recognition)
    if person is None:
        handler.respond_html(
            person_not_found_html(
                person_name,
                face_enabled=handler.server.face_enabled,
                openclip_enabled=handler.server.openclip_enabled,
            ),
            status=HTTPStatus.NOT_FOUND,
        )
        return
    canonical_name = str(person["name"])
    source = person_browser_source(
        canonical_name,
        include_suggestions=person_mode != "confirmed",
        show_faces=show_faces,
    )
    handler.respond_browser_source(
        source,
        page_mode,
        raw_value,
        face_config=handler.server.config.face_recognition,
        hide_out_of_focus=handler.server.hide_out_of_focus,
        item_not_found_message="Filen finnes ikke for denne personen.",
        invalid_page_message="Ugyldig personside.",
    )


def respond_person_references(handler: BildebankRequestHandler, raw_name: str) -> None:
    person_name = urllib.parse.unquote(raw_name).strip()
    if not person_name:
        handler.respond_text("Personnavn mangler.", status=HTTPStatus.BAD_REQUEST)
        return
    person = person_by_name(handler.server.target, person_name, handler.server.config.face_recognition)
    if person is None:
        handler.respond_html(
            person_not_found_html(
                person_name,
                face_enabled=handler.server.face_enabled,
                openclip_enabled=handler.server.openclip_enabled,
            ),
            status=HTTPStatus.NOT_FOUND,
        )
        return
    canonical_name = str(person["name"])
    handler.respond_html(
        person_references_page_html(
            handler.server.target,
            canonical_name,
            handler.server.config.face_recognition,
            openclip_enabled=handler.server.openclip_enabled,
        )
    )


def respond_person_reference_suggestions(handler: BildebankRequestHandler, raw_path: str) -> None:
    raw_name, raw_reference_file_id, page_mode, raw_value = parse_person_reference_suggestions_path(raw_path)
    person_name = urllib.parse.unquote(raw_name).strip()
    if not person_name:
        handler.respond_text("Personnavn mangler.", status=HTTPStatus.BAD_REQUEST)
        return
    try:
        reference_file_id = int(urllib.parse.unquote(raw_reference_file_id).strip())
    except ValueError:
        handler.respond_text("Ugyldig referansebilde.", status=HTTPStatus.BAD_REQUEST)
        return
    if reference_file_id <= 0:
        handler.respond_text("Ugyldig referansebilde.", status=HTTPStatus.BAD_REQUEST)
        return
    person = person_by_name(handler.server.target, person_name, handler.server.config.face_recognition)
    if person is None:
        handler.respond_html(
            person_not_found_html(
                person_name,
                face_enabled=handler.server.face_enabled,
                openclip_enabled=handler.server.openclip_enabled,
            ),
            status=HTTPStatus.NOT_FOUND,
        )
        return
    canonical_name = str(person["name"])
    source = person_reference_suggestions_browser_source(canonical_name, reference_file_id)
    handler.respond_browser_source(
        source,
        page_mode,
        raw_value,
        face_config=handler.server.config.face_recognition,
        hide_out_of_focus=handler.server.hide_out_of_focus,
        item_not_found_message="Filen finnes ikke for dette referansebildet.",
        invalid_page_message="Ugyldig referansebildeside.",
    )


def respond_missing_face_suggestions(handler: BildebankRequestHandler, raw_path: str) -> None:
    source_part, page_mode, raw_value = parse_source_path("missing-suggestions" + raw_path)
    if source_part != "missing-suggestions":
        handler.respond_text("Ugyldig ansiktsside.", status=HTTPStatus.NOT_FOUND)
        return
    handler.respond_browser_source(
        missing_face_suggestions_browser_source(),
        page_mode,
        raw_value,
        face_config=handler.server.config.face_recognition,
        hide_out_of_focus=handler.server.hide_out_of_focus,
        item_not_found_message="Filen finnes ikke i denne ansiktsvisningen.",
        invalid_page_message="Ugyldig ansiktsside.",
    )


def respond_add_face_to_person(handler: BildebankRequestHandler) -> None:
    payload = server_request.read_face_person_payload(handler.headers, handler.rfile)
    if isinstance(payload[0], dict):
        handler.respond_json(payload[0], status=payload[1])
        return
    person_name, face_id = payload
    try:
        config = handler.server.config.face_recognition
        result = add_face_to_person(handler.server.target, person_name, face_id, config)
    except TargetLockError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
        return
    except ValueError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return
    clear_face_caches()
    person_link_url = person_item_url_for_face(handler.server.target, result.person_name, result.face_id, config)
    handler.respond_json(
        {
            "ok": True,
            "person_name": result.person_name,
            "person_url": person_link_url,
            "confirmed": True,
            "face_id": result.face_id,
            "added": result.added,
        }
    )


def respond_remove_face_from_person(handler: BildebankRequestHandler) -> None:
    payload = server_request.read_face_person_payload(handler.headers, handler.rfile)
    if isinstance(payload[0], dict):
        handler.respond_json(payload[0], status=payload[1])
        return
    person_name, face_id = payload
    try:
        config = handler.server.config.face_recognition
        result = remove_face_from_person(handler.server.target, person_name, face_id, config)
    except TargetLockError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
        return
    except ValueError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return
    clear_face_caches()
    handler.respond_json(
        {
            "ok": True,
            "person_name": result.person_name,
            "person_url": person_url(result.person_name),
            "face_id": result.face_id,
            "file_id": result.file_id,
            "redirect_url": f"/item/{result.file_id}",
            "removed": result.removed,
        }
    )


def respond_add_person_to_file(handler: BildebankRequestHandler) -> None:
    payload = server_request.read_json_payload(handler.headers, handler.rfile)
    person_name = str(payload.get("person_name") or "").strip()
    if not person_name:
        handler.respond_json({"ok": False, "error": "Personnavn mangler."}, status=HTTPStatus.BAD_REQUEST)
        return
    try:
        file_id = require_int(payload.get("file_id"), "file_id")
    except ValueError:
        handler.respond_json({"ok": False, "error": "Ugyldig file_id."}, status=HTTPStatus.BAD_REQUEST)
        return
    try:
        config = handler.server.config.face_recognition
        result = add_person_to_file(handler.server.target, person_name, file_id, config)
    except TargetLockError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
        return
    except ValueError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return
    clear_face_caches()
    handler.respond_json(
        {
            "ok": True,
            "person_name": result.person_name,
            "person_url": person_item_url(result.person_name, result.file_id, show_faces=False),
            "confirmed": True,
            "file_id": result.file_id,
            "added": result.added,
        }
    )


def respond_remove_person_from_file(handler: BildebankRequestHandler) -> None:
    payload = server_request.read_json_payload(handler.headers, handler.rfile)
    person_name = str(payload.get("person_name") or "").strip()
    if not person_name:
        handler.respond_json({"ok": False, "error": "Personnavn mangler."}, status=HTTPStatus.BAD_REQUEST)
        return
    try:
        file_id = require_int(payload.get("file_id"), "file_id")
    except ValueError:
        handler.respond_json({"ok": False, "error": "Ugyldig file_id."}, status=HTTPStatus.BAD_REQUEST)
        return
    try:
        config = handler.server.config.face_recognition
        result = remove_person_from_file(handler.server.target, person_name, file_id, config)
    except TargetLockError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
        return
    except ValueError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return
    clear_face_caches()
    handler.respond_json(
        {
            "ok": True,
            "person_name": result.person_name,
            "person_url": person_url(result.person_name),
            "file_id": result.file_id,
            "removed": result.removed,
        }
    )


def respond_create_person_and_add_face(handler: BildebankRequestHandler) -> None:
    payload = server_request.read_face_person_payload(handler.headers, handler.rfile)
    if isinstance(payload[0], dict):
        handler.respond_json(payload[0], status=payload[1])
        return
    person_name, face_id = payload
    try:
        config = handler.server.config.face_recognition
        result = create_person_and_add_face(handler.server.target, person_name, face_id, config)
    except TargetLockError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
        return
    except ValueError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return
    clear_face_caches()
    person_link_url = person_item_url_for_face(handler.server.target, result.person_name, result.face_id, config)
    handler.respond_json(
        {
            "ok": True,
            "person_name": result.person_name,
            "person_url": person_link_url,
            "confirmed": True,
            "face_id": result.face_id,
            "added": result.added,
        }
    )


def respond_rename_person(handler: BildebankRequestHandler) -> None:
    payload = server_request.read_json_payload(handler.headers, handler.rfile)
    old_name = str(payload.get("old_name") or "").strip()
    new_name = str(payload.get("new_name") or "").strip()
    if not old_name:
        handler.respond_json({"ok": False, "error": "Gammelt personnavn mangler."}, status=HTTPStatus.BAD_REQUEST)
        return
    if not new_name:
        handler.respond_json({"ok": False, "error": "Nytt personnavn mangler."}, status=HTTPStatus.BAD_REQUEST)
        return
    try:
        config = handler.server.config.face_recognition
        result = rename_person(handler.server.target, old_name, new_name, config)
    except TargetLockError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
        return
    except ValueError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return
    clear_face_caches()
    handler.respond_json(
        {
            "ok": True,
            "old_name": result.old_name,
            "new_name": result.new_name,
            "person_url": f"{person_url(result.new_name)}/no-faces",
        }
    )


def respond_delete_person(handler: BildebankRequestHandler) -> None:
    payload = server_request.read_json_payload(handler.headers, handler.rfile)
    person_name = str(payload.get("person_name") or "").strip()
    if not person_name:
        handler.respond_json({"ok": False, "error": "Personnavn mangler."}, status=HTTPStatus.BAD_REQUEST)
        return
    try:
        config = handler.server.config.face_recognition
        result = delete_person(handler.server.target, person_name, config)
    except TargetLockError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.CONFLICT)
        return
    except ValueError as exc:
        handler.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        return
    clear_face_caches()
    handler.respond_json(
        {
            "ok": True,
            "person_name": result.person_name,
            "removed_faces": result.removed_faces,
            "removed_files": result.removed_files,
            "removed_suggestions": result.removed_suggestions,
        }
    )
