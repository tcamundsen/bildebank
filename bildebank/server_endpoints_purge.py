from __future__ import annotations

import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from . import server_actions, server_request
from .collection_paths import (
    is_deleted_collection_file_path,
    parse_collection_relative_path,
)
from .file_purge import (
    PendingPurgePreview,
    PurgeConfirmationIdentity,
    PurgeFileResult,
    TombstoneConfirmationIdentity,
    TombstonePreview,
)
from .server_endpoints_items import clear_file_navigation_cache
from .target_lock import TargetLockError

if TYPE_CHECKING:
    from .server_handler import BildebankRequestHandler


_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def respond_preview_file_purge(handler: BildebankRequestHandler) -> None:
    payload = _read_payload(handler)
    if payload is None:
        return
    try:
        file_id = _positive_int(payload.get("file_id"))
    except ValueError:
        _respond_bad_request(handler)
        return
    try:
        preview = server_actions.preview_file_purge_from_browser(
            handler.server.target,
            file_id=file_id,
        )
    except ValueError:
        _respond_state_changed(handler)
        return
    except Exception:  # noqa: BLE001 - purge API must not expose local details
        _respond_operation_failed(handler)
        return
    if preview.count != 1:
        _respond_state_changed(handler)
        return
    handler.respond_json(
        {
            "ok": True,
            "preview": (
                _confirmation_json(preview.new_candidates[0])
                if preview.new_candidates
                else None
            ),
            "pending": (
                _pending_preview_json(preview.pending_candidates[0])
                if preview.pending_candidates
                else None
            ),
        }
    )


def respond_preview_deleted_purges(
    handler: BildebankRequestHandler,
) -> None:
    payload = _read_payload(handler)
    if payload is None:
        return
    if payload:
        _respond_bad_request(handler)
        return
    try:
        preview = (
            server_actions.preview_deleted_file_purges_from_browser(
                handler.server.target
            )
        )
    except ValueError:
        _respond_state_changed(handler)
        return
    except Exception:  # noqa: BLE001 - purge API must not expose local details
        _respond_operation_failed(handler)
        return
    handler.respond_json(
        {
            "ok": True,
            "preview": [_confirmation_json(item) for item in preview.new_candidates],
            "count": len(preview.new_candidates),
            "total_size_bytes": sum(
                item.size_bytes for item in preview.new_candidates
            ),
            "pending_count": len(preview.pending_candidates),
        }
    )


def respond_purge_file(handler: BildebankRequestHandler) -> None:
    payload = _read_payload(handler)
    if payload is None:
        return
    try:
        confirmation = _confirmation_from_json(payload.get("identity"))
    except ValueError:
        _respond_bad_request(handler)
        return
    try:
        result = server_actions.purge_file_from_browser(
            handler.server.target,
            confirmation,
        )
    except TargetLockError:
        _respond_locked(handler)
        return
    except ValueError:
        _respond_state_changed(handler)
        return
    except Exception:  # noqa: BLE001 - purge API must not expose local details
        _respond_operation_failed(handler)
        return
    clear_file_navigation_cache(handler.server)
    if result.status in {"skipped", "integrity-error"}:
        _respond_state_changed(handler, result=result)
        return
    handler.respond_json({"ok": True, "result": _result_json(result)})


def respond_purge_deleted(handler: BildebankRequestHandler) -> None:
    payload = _read_payload(handler)
    if payload is None:
        return
    raw_identities = payload.get("identities")
    if not isinstance(raw_identities, list):
        _respond_bad_request(handler)
        return
    try:
        identities = tuple(
            _confirmation_from_json(item) for item in raw_identities
        )
    except ValueError:
        _respond_bad_request(handler)
        return
    try:
        result = server_actions.purge_deleted_files_from_browser(
            handler.server.target,
            identities,
        )
    except TargetLockError:
        _respond_locked(handler)
        return
    except ValueError:
        _respond_state_changed(handler)
        return
    except Exception:  # noqa: BLE001 - purge API must not expose local details
        _respond_operation_failed(handler)
        return
    clear_file_navigation_cache(handler.server)
    handler.respond_json(
        {
            "ok": True,
            "results": [_result_json(item) for item in result.results],
            "deleted": result.deleted,
            "pending": result.pending,
            "skipped": result.skipped,
            "integrity_errors": result.integrity_errors,
            "partial": result.deleted != len(identities),
        }
    )


def respond_retry_file_purge(handler: BildebankRequestHandler) -> None:
    payload = _read_payload(handler)
    if payload is None:
        return
    try:
        purge_id = _positive_int(payload.get("purge_id"))
    except ValueError:
        _respond_bad_request(handler)
        return
    try:
        result = server_actions.retry_file_purge_from_browser(
            handler.server.target,
            purge_id=purge_id,
        )
    except TargetLockError:
        _respond_locked(handler)
        return
    except ValueError:
        _respond_state_changed(handler)
        return
    except Exception:  # noqa: BLE001 - purge API must not expose local details
        _respond_operation_failed(handler)
        return
    clear_file_navigation_cache(handler.server)
    handler.respond_json({"ok": True, "result": _result_json(result)})


def respond_abort_file_purge(handler: BildebankRequestHandler) -> None:
    payload = _read_payload(handler)
    if payload is None:
        return
    try:
        purge_id = _positive_int(payload.get("purge_id"))
    except ValueError:
        _respond_bad_request(handler)
        return
    try:
        identity = server_actions.abort_file_purge_from_browser(
            handler.server.target,
            purge_id=purge_id,
        )
    except TargetLockError:
        _respond_locked(handler)
        return
    except ValueError:
        _respond_state_changed(handler)
        return
    except Exception:  # noqa: BLE001 - purge API must not expose local details
        _respond_operation_failed(handler)
        return
    clear_file_navigation_cache(handler.server)
    handler.respond_json(
        {"ok": True, "file_id": identity.file_id, "aborted": True}
    )


def respond_file_tombstones(handler: BildebankRequestHandler) -> None:
    try:
        tombstones = server_actions.file_tombstones_for_browser(
            handler.server.target
        )
    except Exception:  # noqa: BLE001 - tombstone API must not expose local details
        handler.respond_json(
            {"ok": False, "error": "Kunne ikke lese slettingsmarkørene."},
            status=HTTPStatus.CONFLICT,
        )
        return
    handler.respond_json(
        {
            "ok": True,
            "tombstones": [_tombstone_display_json(item) for item in tombstones],
        }
    )


def respond_preview_tombstone_removal(
    handler: BildebankRequestHandler,
) -> None:
    payload = _read_payload(handler)
    if payload is None:
        return
    try:
        tombstone_id = _positive_int(payload.get("tombstone_id"))
    except ValueError:
        _respond_bad_request(handler)
        return
    try:
        preview = server_actions.preview_tombstone_removal_from_browser(
            handler.server.target,
            tombstone_id=tombstone_id,
        )
    except ValueError:
        _respond_state_changed(handler)
        return
    except Exception:  # noqa: BLE001 - tombstone API must not expose local details
        _respond_operation_failed(handler)
        return
    handler.respond_json(
        {
            "ok": True,
            "identity": _tombstone_identity_json(preview.identity),
            "tombstone": _tombstone_display_json(preview),
        }
    )


def respond_remove_tombstone(handler: BildebankRequestHandler) -> None:
    payload = _read_payload(handler)
    if payload is None:
        return
    try:
        confirmation = _tombstone_identity_from_json(payload.get("identity"))
    except ValueError:
        _respond_bad_request(handler)
        return
    try:
        server_actions.remove_tombstone_from_browser(
            handler.server.target,
            confirmation,
        )
    except TargetLockError:
        _respond_locked(handler)
        return
    except ValueError:
        _respond_state_changed(handler)
        return
    except Exception:  # noqa: BLE001 - tombstone API must not expose local details
        _respond_operation_failed(handler)
        return
    clear_file_navigation_cache(handler.server)
    handler.respond_json({"ok": True, "removed": True})


def _read_payload(handler: BildebankRequestHandler) -> dict[str, object] | None:
    try:
        return server_request.read_json_payload(handler.headers, handler.rfile)
    except (UnicodeDecodeError, ValueError):
        _respond_bad_request(handler)
        return None


def _confirmation_json(
    identity: PurgeConfirmationIdentity,
) -> dict[str, object]:
    return {
        "file_id": identity.file_id,
        "sha256": identity.sha256,
        "size_bytes": identity.size_bytes,
        "expected_path": identity.expected_path.as_posix(),
        "deleted_at": identity.deleted_at,
    }


def _confirmation_from_json(value: object) -> PurgeConfirmationIdentity:
    if not isinstance(value, dict):
        raise ValueError("Ugyldig identitet.")
    expected_path_value = value.get("expected_path")
    if not isinstance(expected_path_value, str):
        raise ValueError("Ugyldig identitet.")
    expected_path = parse_collection_relative_path(expected_path_value)
    sha256 = value.get("sha256")
    deleted_at = value.get("deleted_at")
    size_bytes = value.get("size_bytes")
    if (
        not is_deleted_collection_file_path(expected_path)
        or not isinstance(sha256, str)
        or _SHA256_RE.fullmatch(sha256) is None
        or type(size_bytes) is not int
        or size_bytes < 0
        or not isinstance(deleted_at, str)
        or not deleted_at
        or len(deleted_at) > 128
    ):
        raise ValueError("Ugyldig identitet.")
    return PurgeConfirmationIdentity(
        file_id=_positive_int(value.get("file_id")),
        sha256=sha256,
        size_bytes=size_bytes,
        expected_path=expected_path,
        deleted_at=deleted_at,
    )


def _pending_preview_json(
    pending: PendingPurgePreview,
) -> dict[str, object]:
    return {
        "purge_id": pending.purge_id,
        "file_id": pending.identity.file_id,
        "size_bytes": pending.identity.size_bytes,
        "expected_path": pending.identity.expected_path.as_posix(),
        "attempts": pending.attempts,
        "original_state": pending.original_state,
    }


def _result_json(result: PurgeFileResult) -> dict[str, object]:
    return {
        "file_id": result.file_id,
        "status": result.status,
        "purge_id": result.purge_id,
    }


def _tombstone_identity_json(
    identity: TombstoneConfirmationIdentity,
) -> dict[str, object]:
    return {
        "tombstone_id": identity.tombstone_id,
        "sha256": identity.sha256,
        "size_bytes": identity.size_bytes,
        "purged_at": identity.purged_at,
    }


def _tombstone_identity_from_json(
    value: object,
) -> TombstoneConfirmationIdentity:
    if not isinstance(value, dict):
        raise ValueError("Ugyldig identitet.")
    sha256 = value.get("sha256")
    size_bytes = value.get("size_bytes")
    purged_at = value.get("purged_at")
    if (
        not isinstance(sha256, str)
        or _SHA256_RE.fullmatch(sha256) is None
        or type(size_bytes) is not int
        or size_bytes < 0
        or not isinstance(purged_at, str)
        or not purged_at
        or len(purged_at) > 128
    ):
        raise ValueError("Ugyldig identitet.")
    return TombstoneConfirmationIdentity(
        tombstone_id=_positive_int(value.get("tombstone_id")),
        sha256=sha256,
        size_bytes=size_bytes,
        purged_at=purged_at,
    )


def _tombstone_display_json(
    preview: TombstonePreview,
) -> dict[str, object]:
    return {
        "id": preview.identity.tombstone_id,
        "original_filename": preview.original_filename,
        "former_target_path": preview.former_target_path.as_posix(),
        "size_bytes": preview.identity.size_bytes,
        "purged_at": preview.identity.purged_at,
    }


def _positive_int(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("Ugyldig positivt heltall.")
    return value


def _respond_bad_request(handler: BildebankRequestHandler) -> None:
    handler.respond_json(
        {"ok": False, "error": "Ugyldig forespørsel."},
        status=HTTPStatus.BAD_REQUEST,
    )


def _respond_locked(handler: BildebankRequestHandler) -> None:
    handler.respond_json(
        {
            "ok": False,
            "error": "Bildesamlingen er opptatt. Prøv igjen.",
        },
        status=HTTPStatus.CONFLICT,
    )


def _respond_operation_failed(handler: BildebankRequestHandler) -> None:
    handler.respond_json(
        {
            "ok": False,
            "error": "Handlingen kunne ikke fullføres. Prøv igjen.",
        },
        status=HTTPStatus.INTERNAL_SERVER_ERROR,
    )


def _respond_state_changed(
    handler: BildebankRequestHandler,
    *,
    result: PurgeFileResult | None = None,
) -> None:
    payload: dict[str, Any] = {
        "ok": False,
        "error": "Tilstanden er endret. Oppdater siden og prøv igjen.",
    }
    if result is not None:
        payload["result"] = _result_json(result)
    handler.respond_json(payload, status=HTTPStatus.CONFLICT)
