from __future__ import annotations

import hashlib
import json
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from bildebank import db, server_endpoints_purge
from bildebank.file_purge import (
    preview_file_purge,
    purge_file,
)
from bildebank.server_assets import SERVER_JS
from bildebank.server_handler import BildebankRequestHandler
from bildebank.server_pages import removed_files_page_html


DELETED_AT = "2026-07-28 12:00:00"


class EndpointHandler:
    def __init__(self, target: Path, payload: object | None = None) -> None:
        data = json.dumps(payload if payload is not None else {}).encode()
        self.headers = {
            "Content-Length": str(len(data)),
            "Content-Type": "application/json",
        }
        self.rfile = BytesIO(data)
        self.server = SimpleNamespace(
            target=target,
            clear_browser_navigation_cache=Mock(),
        )
        self.body: dict[str, object] | None = None
        self.status = HTTPStatus.OK

    def respond_json(
        self,
        content: dict[str, object],
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self.body = content
        self.status = status


def create_deleted_file(
    target: Path,
    *,
    filename: str,
    content: bytes,
) -> tuple[int, Path]:
    relative_path = Path("deleted", "2024", "01", filename)
    path = target / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    conn = db.connect(target)
    try:
        file_id = int(
            conn.execute(
                """
                INSERT INTO files(
                    target_path, target_path_key, original_filename,
                    stored_filename, sha256, size_bytes, taken_date,
                    date_source, deleted_at, deleted_original_target_path
                )
                VALUES(?, ?, ?, ?, ?, ?, '2024-01-02', 'filename', ?, ?)
                RETURNING id
                """,
                (
                    relative_path.as_posix(),
                    db.relative_path_key(relative_path),
                    filename,
                    filename,
                    hashlib.sha256(content).hexdigest(),
                    len(content),
                    DELETED_AT,
                    Path("2024", "01", filename).as_posix(),
                ),
            ).fetchone()["id"]
        )
        source_id = int(
            conn.execute(
                """
                INSERT INTO sources(path, path_key, name, imported_at, status)
                VALUES(?, ?, ?, CURRENT_TIMESTAMP, 'imported')
                RETURNING id
                """,
                (
                    f"C:\\Bilder\\{filename}",
                    f"c:\\bilder\\{filename.casefold()}",
                    f"source-{file_id}",
                ),
            ).fetchone()["id"]
        )
        conn.execute(
            """
            INSERT INTO file_sources(
                file_id, source_id, source_path, source_path_key,
                sha256, size_bytes
            )
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                source_id,
                f"C:\\Bilder\\{filename}",
                f"c:\\bilder\\{filename.casefold()}",
                hashlib.sha256(content).hexdigest(),
                len(content),
            ),
        )
        conn.commit()
        return file_id, path
    finally:
        conn.close()


def create_pending_purge(target: Path, *, filename: str) -> tuple[int, int, Path]:
    file_id, original = create_deleted_file(
        target,
        filename=filename,
        content=f"content-{filename}".encode(),
    )
    confirmation = preview_file_purge(
        target,
        file_id=file_id,
    ).new_candidates[0]
    with patch(
        "bildebank.file_purge._delete_derived_files",
        side_effect=OSError("simulert låst avledet fil"),
    ):
        result = purge_file(target, confirmation)
    assert result.status == "pending"
    assert result.purge_id is not None
    return file_id, result.purge_id, original


def response_body(handler: EndpointHandler) -> dict[str, object]:
    assert handler.body is not None
    return handler.body


def test_removed_page_shows_ordinary_pending_missing_and_tombstone_states(
    tmp_path: Path,
) -> None:
    ordinary_target = tmp_path / "ordinary"
    db.init_database(ordinary_target)
    ordinary_id, _ordinary = create_deleted_file(
        ordinary_target,
        filename="ordinary.jpg",
        content=b"ordinary",
    )

    ordinary_body = removed_files_page_html(ordinary_target)

    assert f'data-purge-preview-item="{ordinary_id}"' in ordinary_body
    assert f'data-undelete-item="{ordinary_id}"' in ordinary_body
    assert "Tøm papirkurven" in ordinary_body
    assert "Hva betyr permanent sletting?" in ordinary_body
    assert 'id="purgeDialog"' in ordinary_body

    pending_target = tmp_path / "pending"
    db.init_database(pending_target)
    pending_id, purge_id, original = create_pending_purge(
        pending_target,
        filename="pending.jpg",
    )

    pending_body = removed_files_page_html(pending_target)

    assert f'data-removed-file-id="{pending_id}"' in pending_body
    assert f'data-purge-retry="{purge_id}"' in pending_body
    assert f'data-purge-abort="{purge_id}"' in pending_body
    assert f'data-undelete-item="{pending_id}"' not in pending_body
    assert "Permanent sletting venter etter en feil" in pending_body

    original.unlink()
    missing_body = removed_files_page_html(pending_target)

    assert "Venter på å fullføre permanent sletting" in missing_body
    assert f'data-purge-retry="{purge_id}"' in missing_body
    assert f'data-purge-abort="{purge_id}"' not in missing_body

    tombstone_target = tmp_path / "tombstone"
    db.init_database(tombstone_target)
    tombstoned_id, _path = create_deleted_file(
        tombstone_target,
        filename="gone.jpg",
        content=b"gone",
    )
    result = purge_file(
        tombstone_target,
        preview_file_purge(
            tombstone_target,
            file_id=tombstoned_id,
        ).new_candidates[0],
    )
    assert result.tombstone_id is not None

    tombstone_body = removed_files_page_html(tombstone_target)

    assert "Slettingsmarkører" in tombstone_body
    assert "gone.jpg" in tombstone_body
    assert "2024/01/gone.jpg" in tombstone_body
    assert (
        f'data-tombstone-preview-remove="{result.tombstone_id}"'
        in tombstone_body
    )


def test_purge_frontend_has_confirmations_retry_and_close_only_partial_result() -> None:
    assert "/api/purge/preview-file" in SERVER_JS
    assert "/api/purge/preview-deleted" in SERVER_JS
    assert "/api/purge/retry" in SERVER_JS
    assert "/api/purge/abort" in SERVER_JS
    assert "/api/tombstone/preview-remove" in SERVER_JS
    assert "Eldre snapshots og andre sikkerhetskopier" in SERVER_JS
    assert 'title: "Enkelte filer kunne ikke slettes."' in SERVER_JS
    assert 'cancelLabel: "Lukk"' in SERVER_JS
    assert "confirmLabel: \"Ja, prøv igjen\"" in SERVER_JS
    assert "cancelLabel: \"Nei\"" in SERVER_JS


def test_single_purge_endpoint_revalidates_identity_and_hides_details(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_id, original = create_deleted_file(
        target,
        filename="image.jpg",
        content=b"original",
    )
    preview_handler = EndpointHandler(target, {"file_id": file_id})

    server_endpoints_purge.respond_preview_file_purge(preview_handler)

    preview = response_body(preview_handler)["preview"]
    assert isinstance(preview, dict)
    assert preview["file_id"] == file_id
    assert preview["expected_path"] == "deleted/2024/01/image.jpg"
    original.write_bytes(b"changed")

    purge_handler = EndpointHandler(target, {"identity": preview})
    server_endpoints_purge.respond_purge_file(purge_handler)

    body = response_body(purge_handler)
    assert purge_handler.status == HTTPStatus.CONFLICT
    assert body["ok"] is False
    error = str(body["error"])
    assert "target" not in error
    assert "image.jpg" not in error
    assert str(preview["sha256"]) not in error
    assert original.read_bytes() == b"changed"


def test_single_purge_endpoint_success_clears_browser_cache(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_id, original = create_deleted_file(
        target,
        filename="image.jpg",
        content=b"original",
    )
    preview_handler = EndpointHandler(target, {"file_id": file_id})
    server_endpoints_purge.respond_preview_file_purge(preview_handler)
    identity = response_body(preview_handler)["preview"]

    purge_handler = EndpointHandler(target, {"identity": identity})
    server_endpoints_purge.respond_purge_file(purge_handler)

    assert purge_handler.status == HTTPStatus.OK
    assert response_body(purge_handler)["result"] == {
        "file_id": file_id,
        "status": "deleted",
        "purge_id": None,
    }
    assert not original.exists()
    purge_handler.server.clear_browser_navigation_cache.assert_called_once_with()


def test_bulk_endpoint_uses_exact_preview_and_reports_partial_result(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    good_id, good = create_deleted_file(
        target,
        filename="good.jpg",
        content=b"good",
    )
    bad_id, bad = create_deleted_file(
        target,
        filename="bad.jpg",
        content=b"bad",
    )
    preview_handler = EndpointHandler(target)
    server_endpoints_purge.respond_preview_deleted_purges(preview_handler)
    identities = response_body(preview_handler)["preview"]
    assert isinstance(identities, list)

    new_id, new = create_deleted_file(
        target,
        filename="new.jpg",
        content=b"new",
    )
    bad.write_bytes(b"changed")
    purge_handler = EndpointHandler(target, {"identities": identities})
    server_endpoints_purge.respond_purge_deleted(purge_handler)

    body = response_body(purge_handler)
    assert body["partial"] is True
    assert body["deleted"] == 1
    assert body["integrity_errors"] == 1
    assert not good.exists()
    assert bad.read_bytes() == b"changed"
    assert new.read_bytes() == b"new"
    result_ids = {
        int(item["file_id"])
        for item in body["results"]  # type: ignore[union-attr]
    }
    assert result_ids == {good_id, bad_id}
    assert new_id not in result_ids
    purge_handler.server.clear_browser_navigation_cache.assert_called_once_with()


def test_retry_and_abort_endpoints_reuse_existing_purge(
    tmp_path: Path,
) -> None:
    abort_target = tmp_path / "abort"
    db.init_database(abort_target)
    file_id, purge_id, original = create_pending_purge(
        abort_target,
        filename="abort.jpg",
    )
    abort_handler = EndpointHandler(abort_target, {"purge_id": purge_id})

    server_endpoints_purge.respond_abort_file_purge(abort_handler)

    assert response_body(abort_handler) == {
        "ok": True,
        "file_id": file_id,
        "aborted": True,
    }
    assert original.exists()
    conn = db.connect(abort_target)
    try:
        assert db.pending_file_purge(conn, purge_id=purge_id) is None
    finally:
        conn.close()
    abort_handler.server.clear_browser_navigation_cache.assert_called_once_with()

    retry_target = tmp_path / "retry"
    db.init_database(retry_target)
    retry_file_id, retry_purge_id, retry_original = create_pending_purge(
        retry_target,
        filename="retry.jpg",
    )
    retry_handler = EndpointHandler(
        retry_target,
        {"purge_id": retry_purge_id},
    )

    server_endpoints_purge.respond_retry_file_purge(retry_handler)

    assert response_body(retry_handler)["result"] == {
        "file_id": retry_file_id,
        "status": "deleted",
        "purge_id": None,
    }
    assert not retry_original.exists()
    retry_handler.server.clear_browser_navigation_cache.assert_called_once_with()


def test_tombstone_list_and_exact_removal_are_safe_and_logged(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_id, _original = create_deleted_file(
        target,
        filename="gone.jpg",
        content=b"gone",
    )
    purge_result = purge_file(
        target,
        preview_file_purge(target, file_id=file_id).new_candidates[0],
    )
    assert purge_result.tombstone_id is not None

    list_handler = EndpointHandler(target)
    server_endpoints_purge.respond_file_tombstones(list_handler)
    tombstones = response_body(list_handler)["tombstones"]
    assert tombstones == [
        {
            "id": purge_result.tombstone_id,
            "original_filename": "gone.jpg",
            "former_target_path": "2024/01/gone.jpg",
            "size_bytes": 4,
            "purged_at": tombstones[0]["purged_at"],  # type: ignore[index]
        }
    ]

    preview_handler = EndpointHandler(
        target,
        {"tombstone_id": purge_result.tombstone_id},
    )
    server_endpoints_purge.respond_preview_tombstone_removal(preview_handler)
    identity = response_body(preview_handler)["identity"]
    assert isinstance(identity, dict)
    stale_identity = dict(identity)
    stale_identity["purged_at"] = f"{identity['purged_at']}-changed"

    stale_handler = EndpointHandler(target, {"identity": stale_identity})
    server_endpoints_purge.respond_remove_tombstone(stale_handler)

    assert stale_handler.status == HTTPStatus.CONFLICT
    conn = db.connect(target)
    try:
        assert db.file_tombstone(
            conn,
            tombstone_id=purge_result.tombstone_id,
        ) is not None
    finally:
        conn.close()

    remove_handler = EndpointHandler(target, {"identity": identity})
    server_endpoints_purge.respond_remove_tombstone(remove_handler)

    assert response_body(remove_handler) == {"ok": True, "removed": True}
    remove_handler.server.clear_browser_navigation_cache.assert_called_once_with()
    conn = db.connect(target)
    try:
        assert db.file_tombstone(
            conn,
            tombstone_id=purge_result.tombstone_id,
        ) is None
        log = conn.execute(
            """
            SELECT command, args_json
            FROM command_log
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        assert log["command"] == "remove-file-tombstone"
        assert json.loads(log["args_json"]) == {"removed": 1, "requested": 1}
        assert "gone.jpg" not in log["args_json"]
        assert str(identity["sha256"]) not in log["args_json"]
    finally:
        conn.close()


def test_purge_endpoint_hides_unexpected_exception_details(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_id, _original = create_deleted_file(
        target,
        filename="image.jpg",
        content=b"original",
    )
    preview_handler = EndpointHandler(target, {"file_id": file_id})
    server_endpoints_purge.respond_preview_file_purge(preview_handler)
    identity = response_body(preview_handler)["preview"]
    detail = f"{target}/bildebank.sqlite: files table failed"
    handler = EndpointHandler(target, {"identity": identity})

    with patch(
        "bildebank.server_endpoints_purge.server_actions.purge_file_from_browser",
        side_effect=RuntimeError(detail),
    ):
        server_endpoints_purge.respond_purge_file(handler)

    assert handler.status == HTTPStatus.INTERNAL_SERVER_ERROR
    error = str(response_body(handler)["error"])
    assert str(target) not in error
    assert "sqlite" not in error
    assert "files table" not in error


@pytest.mark.parametrize(
    "path",
    [
        "/api/purge/preview-file",
        "/api/purge/preview-deleted",
        "/api/purge/file",
        "/api/purge/deleted",
        "/api/purge/retry",
        "/api/purge/abort",
        "/api/tombstone/preview-remove",
        "/api/tombstone/remove",
    ],
)
def test_purge_mutations_require_post(path: str) -> None:
    handler = object.__new__(BildebankRequestHandler)
    handler.path = path
    handler.server = SimpleNamespace(read_only=False, slideshow=None)
    handler.respond_json = Mock()  # type: ignore[method-assign]

    BildebankRequestHandler.do_GET(handler)

    handler.respond_json.assert_called_once_with(
        {"ok": False, "error": "Endepunktet krever POST."},
        status=HTTPStatus.METHOD_NOT_ALLOWED,
    )


def test_purge_post_requires_writable_mode_and_csrf(tmp_path: Path) -> None:
    read_only = object.__new__(BildebankRequestHandler)
    read_only.path = "/api/purge/file"
    read_only.server = SimpleNamespace(read_only=True, slideshow=None)
    read_only.respond_json = Mock()  # type: ignore[method-assign]

    BildebankRequestHandler.do_POST(read_only)

    read_only.respond_json.assert_called_once_with(
        {"ok": False, "error": "Serveren kjører i read-only-modus."},
        status=HTTPStatus.FORBIDDEN,
    )

    no_csrf = object.__new__(BildebankRequestHandler)
    no_csrf.path = "/api/purge/file"
    no_csrf.server = SimpleNamespace(
        target=tmp_path,
        read_only=False,
        slideshow=None,
        csrf_token="test-token",
    )
    no_csrf.headers = {"Content-Length": "0"}  # type: ignore[assignment]
    no_csrf.rfile = BytesIO()
    no_csrf.respond_json = Mock()  # type: ignore[method-assign]

    BildebankRequestHandler.do_POST(no_csrf)

    no_csrf.respond_json.assert_called_once_with(
        {"ok": False, "error": "Ugyldig eller manglende CSRF-token."},
        status=HTTPStatus.FORBIDDEN,
    )


def test_tombstone_list_is_unavailable_in_read_only_mode() -> None:
    handler = object.__new__(BildebankRequestHandler)
    handler.path = "/api/tombstones"
    handler.server = SimpleNamespace(read_only=True, slideshow=None)
    handler.respond_json = Mock()  # type: ignore[method-assign]

    BildebankRequestHandler.do_GET(handler)

    handler.respond_json.assert_called_once_with(
        {"ok": False, "error": "Serveren kjører i read-only-modus."},
        status=HTTPStatus.FORBIDDEN,
    )
