from __future__ import annotations

import sqlite3

import pytest


pytest.importorskip("mcp")


def test_schema_summary_uses_generated_schema_when_example_database_is_stale(
    tmp_path, monkeypatch
) -> None:
    from bildebank import db, dev_mcp

    stale_example_target = tmp_path / "stale-example"
    db.init_database(stale_example_target)
    stale_example = db.db_path_for_target(stale_example_target)
    with sqlite3.connect(stale_example) as conn:
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", ("10",))

    monkeypatch.setattr(dev_mcp, "EXAMPLE_DATABASE", stale_example)

    summary = dev_mcp.get_schema_summary()

    assert summary["schema_source"] == "generated_runtime_schema"
    assert summary["example_schema_version"] == 10
    assert summary["runtime_schema_version"] == db.SCHEMA_VERSION
    assert summary["schema_version"] == db.SCHEMA_VERSION

    tables = {table["name"]: table for table in summary["tables"]}
    file_columns = {column["name"] for column in tables["files"]["columns"]}
    file_source_columns = {column["name"] for column in tables["file_sources"]["columns"]}
    assert "metadata_datetime" in file_columns
    assert "source_path_key" in file_source_columns
    assert "recorded_at" in file_source_columns
    assert "pending_file_moves" in tables


def test_schema_summary_uses_example_database_when_it_is_current(tmp_path, monkeypatch) -> None:
    from bildebank import db, dev_mcp

    current_example_target = tmp_path / "current-example"
    db.init_database(current_example_target)
    current_example = db.db_path_for_target(current_example_target)

    monkeypatch.setattr(dev_mcp, "EXAMPLE_DATABASE", current_example)

    summary = dev_mcp.get_schema_summary()

    assert summary["schema_source"] == "example_database"
    assert summary["example_schema_version"] == db.SCHEMA_VERSION
    assert summary["runtime_schema_version"] == db.SCHEMA_VERSION
    assert summary["schema_version"] == db.SCHEMA_VERSION
