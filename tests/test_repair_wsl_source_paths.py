from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from bildebank import db


def load_repair_module():
    path = Path(__file__).resolve().parents[1] / "tools" / "repair_wsl_source_paths.py"
    spec = importlib.util.spec_from_file_location("repair_wsl_source_paths", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def insert_source_file(
    conn,
    *,
    source_id: int,
    file_id: int,
    source_path: str,
    source_path_key: str,
    sha256: str = "hash",
    size_bytes: int = 4,
) -> None:
    conn.execute(
        """
        INSERT INTO file_sources(
            file_id, source_id, source_path, source_path_key, sha256, size_bytes
        ) VALUES(?, ?, ?, ?, ?, ?)
        """,
        (file_id, source_id, source_path, source_path_key, sha256, size_bytes),
    )


def create_repair_database(tmp_path: Path, *, source_path: str = r"C:\Users\Tom\google") -> tuple[Path, int, int]:
    target = tmp_path / "samling"
    db.init_database(target)
    conn = db.connect(target)
    try:
        source_id = int(
            conn.execute(
                """
                INSERT INTO sources(path, path_key, name, imported_at, status)
                VALUES(?, ?, 'google25', CURRENT_TIMESTAMP, 'imported')
                RETURNING id
                """,
                (source_path, source_path.casefold()),
            ).fetchone()["id"]
        )
        file_id = int(
            conn.execute(
                """
                INSERT INTO files(
                    target_path, target_path_key, original_filename, stored_filename,
                    sha256, size_bytes, taken_date, date_source
                ) VALUES('2025/01/a.jpg', '2025/01/a.jpg', 'a.jpg', 'a.jpg',
                         'hash', 4, '2025-01-01', 'filename')
                RETURNING id
                """
            ).fetchone()["id"]
        )
        conn.commit()
    finally:
        conn.close()
    return target, source_id, file_id


def test_wsl_path_to_windows_handles_slashes_and_backslashes() -> None:
    repair = load_repair_module()

    assert repair.wsl_path_to_windows("/mnt/c/Users/Tom/google/a.jpg") == r"C:\Users\Tom\google\a.jpg"
    assert repair.wsl_path_to_windows(r"\mnt\c\Users\Tom\google\a.jpg") == r"C:\Users\Tom\google\a.jpg"
    assert repair.wsl_path_to_windows(r"C:\Users\Tom\google\a.jpg") is None


def test_plan_deletes_verified_wsl_duplicate(tmp_path: Path) -> None:
    repair = load_repair_module()
    target, source_id, file_id = create_repair_database(tmp_path)
    windows_path = r"C:\Users\Tom\google\a.jpg"
    conn = db.connect(target)
    try:
        insert_source_file(
            conn,
            source_id=source_id,
            file_id=file_id,
            source_path=windows_path,
            source_path_key=repair.windows_path_key(windows_path),
        )
        insert_source_file(
            conn,
            source_id=source_id,
            file_id=file_id,
            source_path="/mnt/c/Users/Tom/google/a.jpg",
            source_path_key="/mnt/c/Users/Tom/google/a.jpg",
        )
        conn.commit()

        plan = repair.build_repair_plan(conn, "google25")

        assert plan.total_rows == 2
        assert plan.wsl_rows == 1
        assert plan.delete_count == 1
        assert plan.update_count == 0
        repair.apply_repair_plan(conn, plan)
        conn.commit()
        rows = conn.execute(
            "SELECT source_path FROM file_sources WHERE source_id = ?",
            (source_id,),
        ).fetchall()
        assert [row["source_path"] for row in rows] == [windows_path]
    finally:
        conn.close()


def test_plan_converts_wsl_row_without_windows_duplicate(tmp_path: Path) -> None:
    repair = load_repair_module()
    target, source_id, file_id = create_repair_database(
        tmp_path,
        source_path="/mnt/c/Users/Tom/google",
    )
    conn = db.connect(target)
    try:
        insert_source_file(
            conn,
            source_id=source_id,
            file_id=file_id,
            source_path="/mnt/c/Users/Tom/google/a.jpg",
            source_path_key="/mnt/c/Users/Tom/google/a.jpg",
        )
        conn.commit()

        plan = repair.build_repair_plan(conn, "google25")

        assert plan.delete_count == 0
        assert plan.update_count == 1
        assert plan.source_path_update == (
            r"C:\Users\Tom\google",
            r"c:\users\tom\google",
        )
        repair.apply_repair_plan(conn, plan)
        conn.commit()
        row = conn.execute(
            "SELECT source_path, source_path_key FROM file_sources WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        assert row["source_path"] == r"C:\Users\Tom\google\a.jpg"
        assert row["source_path_key"] == r"c:\users\tom\google\a.jpg"
        source = conn.execute("SELECT path, path_key FROM sources WHERE id = ?", (source_id,)).fetchone()
        assert source["path"] == r"C:\Users\Tom\google"
        assert source["path_key"] == r"c:\users\tom\google"
    finally:
        conn.close()


def test_plan_rejects_conflicting_duplicate_metadata(tmp_path: Path) -> None:
    repair = load_repair_module()
    target, source_id, file_id = create_repair_database(tmp_path)
    windows_path = r"C:\Users\Tom\google\a.jpg"
    conn = db.connect(target)
    try:
        insert_source_file(
            conn,
            source_id=source_id,
            file_id=file_id,
            source_path=windows_path,
            source_path_key=repair.windows_path_key(windows_path),
        )
        insert_source_file(
            conn,
            source_id=source_id,
            file_id=file_id,
            source_path="/mnt/c/Users/Tom/google/a.jpg",
            source_path_key="/mnt/c/Users/Tom/google/a.jpg",
            sha256="annen-hash",
        )
        conn.commit()

        with pytest.raises(ValueError, match="motstridende"):
            repair.build_repair_plan(conn, "google25")
    finally:
        conn.close()
