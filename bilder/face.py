from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import db
from .config import FaceRecognitionConfig
from .media import IMAGE_EXTENSIONS


FACE_DB_FILENAME = ".bilder-faces.sqlite3"
FACE_SCHEMA_VERSION = 1


@dataclass
class FaceScanStats:
    checked: int = 0
    skipped: int = 0
    scanned: int = 0
    faces: int = 0
    errors: int = 0


def face_db_path(target: Path) -> Path:
    return target / FACE_DB_FILENAME


def connect_face_db(target: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(face_db_path(target))
    conn.row_factory = sqlite3.Row
    apply_face_schema(conn)
    conn.commit()
    return conn


def apply_face_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS scanned_files (
            file_id INTEGER PRIMARY KEY,
            target_path TEXT NOT NULL,
            target_path_key TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL,
            error_message TEXT,
            face_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS faces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            target_path_key TEXT NOT NULL,
            bbox_x REAL NOT NULL,
            bbox_y REAL NOT NULL,
            bbox_width REAL NOT NULL,
            bbox_height REAL NOT NULL,
            detection_score REAL NOT NULL,
            embedding_model TEXT NOT NULL,
            embedding BLOB NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_faces_file_id ON faces(file_id);
        CREATE INDEX IF NOT EXISTS idx_faces_target_path_key ON faces(target_path_key);
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(FACE_SCHEMA_VERSION),),
    )


def face_db_summary(target: Path) -> tuple[bool, int, int]:
    path = face_db_path(target)
    if not path.exists():
        return False, 0, 0
    conn = sqlite3.connect(path)
    try:
        scanned = count_rows(conn, "scanned_files")
        faces = count_rows(conn, "faces")
        return True, scanned, faces
    finally:
        conn.close()


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    if table not in {"scanned_files", "faces"}:
        raise ValueError(f"Ukjent face-tabell: {table}")
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None:
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def scan_faces(target: Path, config: FaceRecognitionConfig, *, limit: int | None = None) -> FaceScanStats:
    stats = FaceScanStats()
    app = load_face_app(config)
    main_conn = db.connect(target)
    face_conn = connect_face_db(target)
    try:
        for row in active_image_files(main_conn, limit=limit):
            stats.checked += 1
            file_id = int(row["id"])
            target_path = Path(str(row["target_path"]))
            target_path_key = str(row["target_path_key"])
            sha256 = str(row["sha256"])
            if is_file_scanned(face_conn, file_id, sha256):
                stats.skipped += 1
                continue
            try:
                image = read_image(target_path)
                if image is None:
                    raise ValueError(f"Kunne ikke lese bildefil: {target_path}")
                faces = app.get(image)
                replace_file_faces(
                    face_conn,
                    file_id=file_id,
                    target_path=target_path,
                    target_path_key=target_path_key,
                    sha256=sha256,
                    faces=faces,
                    embedding_model=config.model_name,
                )
                stats.scanned += 1
                stats.faces += len(faces)
            except Exception as exc:  # noqa: BLE001 - scan should continue and record failures
                mark_file_scan_error(
                    face_conn,
                    file_id=file_id,
                    target_path=target_path,
                    target_path_key=target_path_key,
                    sha256=sha256,
                    message=str(exc),
                )
                stats.errors += 1
            face_conn.commit()
    finally:
        main_conn.close()
        face_conn.close()
    return stats


def active_image_files(conn: sqlite3.Connection, *, limit: int | None = None) -> list[sqlite3.Row]:
    sql = """
        SELECT id, target_path, target_path_key, sha256, stored_filename
        FROM files
        WHERE deleted_at IS NULL
        ORDER BY imported_at, id
    """
    rows = []
    for row in conn.execute(sql):
        if Path(str(row["stored_filename"])).suffix.lower() in IMAGE_EXTENSIONS:
            rows.append(row)
            if limit is not None and len(rows) >= limit:
                break
    return rows


def is_file_scanned(conn: sqlite3.Connection, file_id: int, sha256: str) -> bool:
    row = conn.execute(
        "SELECT sha256, status FROM scanned_files WHERE file_id = ?",
        (file_id,),
    ).fetchone()
    return row is not None and row["sha256"] == sha256 and row["status"] == "ok"


def replace_file_faces(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    target_path: Path,
    target_path_key: str,
    sha256: str,
    faces: list[Any],
    embedding_model: str,
) -> None:
    conn.execute("DELETE FROM faces WHERE file_id = ?", (file_id,))
    for face in faces:
        x1, y1, x2, y2 = face_bbox(face)
        conn.execute(
            """
            INSERT INTO faces(
                file_id, target_path_key, bbox_x, bbox_y, bbox_width, bbox_height,
                detection_score, embedding_model, embedding
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                target_path_key,
                x1,
                y1,
                x2 - x1,
                y2 - y1,
                face_score(face),
                embedding_model,
                embedding_blob(face),
            ),
        )
    conn.execute(
        """
        INSERT INTO scanned_files(
            file_id, target_path, target_path_key, sha256, status, error_message, face_count
        ) VALUES(?, ?, ?, ?, 'ok', NULL, ?)
        ON CONFLICT(file_id) DO UPDATE SET
            target_path = excluded.target_path,
            target_path_key = excluded.target_path_key,
            sha256 = excluded.sha256,
            scanned_at = CURRENT_TIMESTAMP,
            status = 'ok',
            error_message = NULL,
            face_count = excluded.face_count
        """,
        (file_id, str(target_path), target_path_key, sha256, len(faces)),
    )


def mark_file_scan_error(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    target_path: Path,
    target_path_key: str,
    sha256: str,
    message: str,
) -> None:
    conn.execute("DELETE FROM faces WHERE file_id = ?", (file_id,))
    conn.execute(
        """
        INSERT INTO scanned_files(
            file_id, target_path, target_path_key, sha256, status, error_message, face_count
        ) VALUES(?, ?, ?, ?, 'error', ?, 0)
        ON CONFLICT(file_id) DO UPDATE SET
            target_path = excluded.target_path,
            target_path_key = excluded.target_path_key,
            sha256 = excluded.sha256,
            scanned_at = CURRENT_TIMESTAMP,
            status = 'error',
            error_message = excluded.error_message,
            face_count = 0
        """,
        (file_id, str(target_path), target_path_key, sha256, message[:1000]),
    )


def load_face_app(config: FaceRecognitionConfig):
    try:
        from insightface.app import FaceAnalysis
    except ImportError as exc:
        raise ValueError(
            "InsightFace er ikke installert. Kjør install-insightface.ps1 fra programmappen."
        ) from exc
    providers = ["CPUExecutionProvider"] if config.provider == "cpu" else None
    app = FaceAnalysis(name=config.model_name, root=str(config.model_root), providers=providers)
    app.prepare(ctx_id=-1 if config.provider == "cpu" else 0, det_size=(640, 640))
    return app


def read_image(path: Path):
    try:
        import cv2
    except ImportError as exc:
        raise ValueError("OpenCV mangler. Installer InsightFace-komponenten på nytt.") from exc
    return cv2.imread(str(path))


def face_bbox(face: Any) -> tuple[float, float, float, float]:
    bbox = getattr(face, "bbox")
    return float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])


def face_score(face: Any) -> float:
    return float(getattr(face, "det_score", 0.0))


def embedding_blob(face: Any) -> bytes:
    embedding = getattr(face, "embedding")
    if hasattr(embedding, "astype"):
        return embedding.astype("float32").tobytes()
    import array

    values = array.array("f", [float(value) for value in embedding])
    return values.tobytes()
