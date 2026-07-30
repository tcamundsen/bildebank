from __future__ import annotations

import json
import sqlite3
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from bildebank import db, server_endpoints_browser, server_endpoints_items
from bildebank.server_assets import SERVER_JS
from bildebank.server_browser_queries import browser_item_by_id, browser_month_navigation
from bildebank.server_pages import item_page_html
from tests.db_test_helpers import insert_test_file


class ViewEndpointHandler:
    def __init__(self, target: Path, payload: object) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.headers = {
            "Content-Length": str(len(data)),
            "Content-Type": "application/json",
        }
        self.rfile = BytesIO(data)
        self.server = SimpleNamespace(target=target)
        self.status: HTTPStatus | None = None
        self.body: dict[str, object] | None = None

    def respond_empty(self, *, status: HTTPStatus = HTTPStatus.NO_CONTENT) -> None:
        self.status = status

    def respond_json(
        self,
        content: dict[str, object],
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self.status = status
        self.body = content


def test_v21_migrates_file_view_stats_without_changing_files(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_id = insert_test_file(target, "2024/01/seen.png")
    conn = db.connect(target)
    try:
        conn.execute("DROP TABLE file_view_stats")
        conn.execute("UPDATE meta SET value = '21' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()

    plan = db.migration_plan(target)
    result = db.migrate_database(target)

    assert plan.creates_file_view_stats
    assert result.creates_file_view_stats
    conn = db.connect(target)
    try:
        assert db.schema_version(conn) == 22
        assert conn.execute("SELECT id FROM files").fetchone()["id"] == file_id
        assert conn.execute("SELECT * FROM file_view_stats").fetchall() == []
    finally:
        conn.close()


def test_record_file_view_updates_active_file_and_cascades_on_delete(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_id = insert_test_file(target, "2024/01/viewed.png")
    conn = db.connect(target)
    try:
        assert db.record_file_view(conn, file_id=file_id, viewed_at="2026-07-30 10:00:00")
        assert db.record_file_view(conn, file_id=file_id, viewed_at="2026-07-30 10:00:05")
        conn.commit()
        row = conn.execute("SELECT * FROM file_view_stats WHERE file_id = ?", (file_id,)).fetchone()
        assert dict(row) == {
            "file_id": file_id,
            "view_count": 2,
            "first_viewed_at": "2026-07-30 10:00:00",
            "last_viewed_at": "2026-07-30 10:00:05",
        }
        conn.execute("UPDATE files SET deleted_at = '2026-07-30 11:00:00' WHERE id = ?", (file_id,))
        assert not db.record_file_view(conn, file_id=file_id, viewed_at="2026-07-30 11:00:05")
        conn.execute("UPDATE files SET deleted_at = NULL WHERE id = ?", (file_id,))
        assert db.record_file_view(conn, file_id=file_id, viewed_at="2026-07-30 11:00:10")
        conn.commit()
        assert conn.execute(
            "SELECT view_count FROM file_view_stats WHERE file_id = ?", (file_id,)
        ).fetchone()["view_count"] == 3
        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        conn.commit()
        assert conn.execute("SELECT * FROM file_view_stats").fetchall() == []
    finally:
        conn.close()


def test_record_file_view_ignores_unknown_and_deleted_files(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    deleted_id = insert_test_file(target, "deleted/2024/01/deleted.png", deleted=True)
    conn = db.connect(target)
    try:
        assert not db.record_file_view(conn, file_id=999, viewed_at="2026-07-30 10:00:00")
        assert not db.record_file_view(conn, file_id=deleted_id, viewed_at="2026-07-30 10:00:00")
        conn.commit()
        assert conn.execute("SELECT * FROM file_view_stats").fetchall() == []
    finally:
        conn.close()


def test_random_view_candidate_prefers_unseen_media_and_excludes_deleted(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    seen_id = insert_test_file(target, "2024/01/seen.png")
    unseen_video_id = insert_test_file(target, "2024/01/unseen.mp4")
    insert_test_file(target, "2024/01/raw.nef")
    insert_test_file(target, "deleted/2024/01/deleted.png", deleted=True)
    conn = db.connect(target)
    try:
        db.record_file_view(conn, file_id=seen_id, viewed_at="2026-07-01 10:00:00")
        conn.commit()
        assert db.random_view_candidate_file_id(conn, choose=lambda ids: ids[0]) == unseen_video_id
    finally:
        conn.close()


def test_random_view_candidate_uses_oldest_pool_after_everything_is_seen(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_ids = [insert_test_file(target, f"2024/01/{index}.png") for index in range(21)]
    conn = db.connect(target)
    try:
        for index, file_id in enumerate(file_ids):
            db.record_file_view(conn, file_id=file_id, viewed_at=f"2026-07-30 10:{index:02}:00")
        conn.commit()
        assert db.random_view_candidate_file_id(conn, choose=lambda ids: ids[-1]) == file_ids[19]
    finally:
        conn.close()


def test_item_viewed_endpoint_records_a_view_and_hides_busy_database(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_id = insert_test_file(target, "2024/01/viewed.png")

    handler = ViewEndpointHandler(target, {"file_id": file_id})
    server_endpoints_items.respond_item_viewed(handler)  # type: ignore[arg-type]
    assert handler.status == HTTPStatus.OK
    assert handler.body == {"recorded": True}
    conn = db.connect(target)
    try:
        assert conn.execute("SELECT view_count FROM file_view_stats").fetchone()["view_count"] == 1
    finally:
        conn.close()

    lock_connection = sqlite3.connect(target / db.DB_FILENAME)
    try:
        lock_connection.execute("BEGIN EXCLUSIVE")
        busy_handler = ViewEndpointHandler(target, {"file_id": file_id})
        server_endpoints_items.respond_item_viewed(busy_handler)  # type: ignore[arg-type]
        assert busy_handler.status == HTTPStatus.NO_CONTENT
    finally:
        lock_connection.rollback()
        lock_connection.close()

    (target / ".bildebank.lock").write_text("opptatt")
    try:
        locked_handler = ViewEndpointHandler(target, {"file_id": file_id})
        server_endpoints_items.respond_item_viewed(locked_handler)  # type: ignore[arg-type]
        assert locked_handler.status == HTTPStatus.NO_CONTENT
    finally:
        (target / ".bildebank.lock").unlink()

    invalid_handler = ViewEndpointHandler(target, {"file_id": 0})
    server_endpoints_items.respond_item_viewed(invalid_handler)  # type: ignore[arg-type]
    assert invalid_handler.status == HTTPStatus.BAD_REQUEST


def test_random_endpoint_and_item_page_client_markup(tmp_path: Path) -> None:
    target = tmp_path / "target"
    db.init_database(target)
    file_id = insert_test_file(target, "2024/01/random.png")

    response: dict[str, object] = {}
    handler = SimpleNamespace(
        server=SimpleNamespace(target=target),
        redirect=lambda location: response.update(location=location),
        respond_text=lambda content, *, status: response.update(content=content, status=status),
    )
    server_endpoints_browser.respond_random_item(handler)
    assert response["location"] == f"/item/{file_id}"

    item = browser_item_by_id(target, file_id)
    assert item is not None
    month_navigation = browser_month_navigation(target, item)
    page = item_page_html(target, item, None, None, month_navigation)
    read_only_page = item_page_html(
        target,
        item,
        None,
        None,
        month_navigation,
        read_only=True,
    )
    assert 'data-view-registration-enabled="true"' in page
    assert "data-view-registration-enabled" not in read_only_page
    assert 'href="/random">Tilfeldig bilde</a>' in page
    assert 'data-view-status hidden aria-live="polite">(sett)</span>' in page
    assert "/api/item-viewed" in SERVER_JS
    assert "payload?.recorded === true" in SERVER_JS
    assert "video.addEventListener(\"seeking\"" in SERVER_JS
