from __future__ import annotations

import sqlite3
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import db
from .config import FaceRecognitionConfig
from .media import IMAGE_EXTENSIONS, image_dimensions


FACE_DB_FILENAME = ".bilder-faces.sqlite3"
FACE_SCHEMA_VERSION = 1


@dataclass
class FaceScanStats:
    checked: int = 0
    skipped: int = 0
    scanned: int = 0
    faces: int = 0
    errors: int = 0


@dataclass(frozen=True)
class FaceReport:
    database_exists: bool
    scanned_files: int = 0
    total_faces: int = 0
    files_with_zero_faces: int = 0
    files_with_one_face: int = 0
    files_with_multiple_faces: int = 0
    scan_errors: int = 0
    top_files: tuple[sqlite3.Row, ...] = ()
    errors: tuple[sqlite3.Row, ...] = ()


@dataclass(frozen=True)
class FaceGroupStats:
    faces: int
    groups: int
    grouped_faces: int
    threshold: float


@dataclass(frozen=True)
class AddGroupToPersonResult:
    person_name: str
    group_index: int
    added_faces: int
    already_linked_faces: int


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

        CREATE TABLE IF NOT EXISTS face_group_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            threshold REAL NOT NULL,
            method TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS face_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES face_group_runs(id) ON DELETE CASCADE,
            group_index INTEGER NOT NULL,
            member_count INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS face_group_members (
            group_id INTEGER NOT NULL REFERENCES face_groups(id) ON DELETE CASCADE,
            face_id INTEGER NOT NULL,
            similarity REAL NOT NULL,
            PRIMARY KEY(group_id, face_id)
        );

        CREATE INDEX IF NOT EXISTS idx_face_groups_run_id ON face_groups(run_id);
        CREATE INDEX IF NOT EXISTS idx_face_group_members_face_id ON face_group_members(face_id);

        CREATE TABLE IF NOT EXISTS persons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS person_faces (
            person_id INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
            face_id INTEGER NOT NULL,
            confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(person_id, face_id)
        );

        CREATE INDEX IF NOT EXISTS idx_person_faces_face_id ON person_faces(face_id);
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


def create_person(target: Path, name: str) -> int:
    clean_name = normalize_person_name(name)
    conn = connect_face_db(target)
    try:
        row = conn.execute("SELECT id FROM persons WHERE name = ?", (clean_name,)).fetchone()
        if row is not None:
            return int(row["id"])
        person_id = int(
            conn.execute(
                "INSERT INTO persons(name) VALUES(?) RETURNING id",
                (clean_name,),
            ).fetchone()["id"]
        )
        conn.commit()
        return person_id
    finally:
        conn.close()


def add_group_to_person(target: Path, person_name: str, group_index: int) -> AddGroupToPersonResult:
    clean_name = normalize_person_name(person_name)
    conn = connect_face_db(target)
    try:
        person_id = ensure_person(conn, clean_name)
        group = latest_group_by_index(conn, group_index)
        if group is None:
            raise ValueError(f"Fant ikke ansiktsgruppe {group_index}. Kjør bildebank face-group først.")
        face_ids = [
            int(row["face_id"])
            for row in conn.execute(
                "SELECT face_id FROM face_group_members WHERE group_id = ? ORDER BY face_id",
                (int(group["id"]),),
            )
        ]
        added = 0
        already = 0
        for face_id in face_ids:
            cur = conn.execute(
                "INSERT OR IGNORE INTO person_faces(person_id, face_id) VALUES(?, ?)",
                (person_id, face_id),
            )
            if cur.rowcount:
                added += 1
            else:
                already += 1
        conn.execute("UPDATE persons SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (person_id,))
        conn.commit()
        return AddGroupToPersonResult(clean_name, group_index, added, already)
    finally:
        conn.close()


def list_persons(target: Path) -> list[sqlite3.Row]:
    path = face_db_path(target)
    if not path.exists():
        return []
    conn = connect_face_db(target)
    try:
        return list(
            conn.execute(
                """
                SELECT
                    persons.name,
                    persons.created_at,
                    persons.updated_at,
                    COUNT(person_faces.face_id) AS face_count
                FROM persons
                LEFT JOIN person_faces ON person_faces.person_id = persons.id
                GROUP BY persons.id
                ORDER BY persons.name
                """
            )
        )
    finally:
        conn.close()


def normalize_person_name(name: str) -> str:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("Personnavn kan ikke være tomt.")
    return clean_name


def ensure_person(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM persons WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return int(row["id"])
    return int(conn.execute("INSERT INTO persons(name) VALUES(?) RETURNING id", (name,)).fetchone()["id"])


def latest_group_by_index(conn: sqlite3.Connection, group_index: int) -> sqlite3.Row | None:
    run = conn.execute("SELECT id FROM face_group_runs ORDER BY id DESC LIMIT 1").fetchone()
    if run is None:
        return None
    return conn.execute(
        "SELECT * FROM face_groups WHERE run_id = ? AND group_index = ?",
        (int(run["id"]), group_index),
    ).fetchone()


def face_report(target: Path, *, limit: int = 20) -> FaceReport:
    path = face_db_path(target)
    if not path.exists():
        return FaceReport(database_exists=False)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return FaceReport(
            database_exists=True,
            scanned_files=count_rows(conn, "scanned_files"),
            total_faces=count_rows(conn, "faces"),
            files_with_zero_faces=count_scanned_files(conn, "status = 'ok' AND face_count = 0"),
            files_with_one_face=count_scanned_files(conn, "status = 'ok' AND face_count = 1"),
            files_with_multiple_faces=count_scanned_files(conn, "status = 'ok' AND face_count > 1"),
            scan_errors=count_scanned_files(conn, "status = 'error'"),
            top_files=tuple(
                conn.execute(
                    """
                    SELECT target_path, face_count
                    FROM scanned_files
                    WHERE status = 'ok' AND face_count > 0
                    ORDER BY face_count DESC, target_path
                    LIMIT ?
                    """,
                    (limit,),
                )
            ),
            errors=tuple(
                conn.execute(
                    """
                    SELECT target_path, error_message
                    FROM scanned_files
                    WHERE status = 'error'
                    ORDER BY scanned_at DESC, target_path
                    LIMIT ?
                    """,
                    (limit,),
                )
            ),
        )
    finally:
        conn.close()


def export_face_browser(target: Path, output: Path | None = None) -> Path:
    output_path = output or (target / "faces.html")
    path = face_db_path(target)
    if not path.exists():
        raise ValueError("Face-database finnes ikke. Kjør bildebank face-scan først.")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        items = face_browser_items(target, conn)
    finally:
        conn.close()
    output_path.write_text(render_face_browser_html(items), encoding="utf-8", newline="\n")
    return output_path


def group_faces(target: Path, *, threshold: float = 0.6) -> FaceGroupStats:
    path = face_db_path(target)
    if not path.exists():
        raise ValueError("Face-database finnes ikke. Kjør bildebank face-scan først.")
    conn = connect_face_db(target)
    try:
        face_rows = list(
            conn.execute(
                """
                SELECT id, embedding
                FROM faces
                ORDER BY id
                """
            )
        )
        vectors = [(int(row["id"]), embedding_from_blob(bytes(row["embedding"]))) for row in face_rows]
        parent = {face_id: face_id for face_id, _vector in vectors}
        similarities: dict[tuple[int, int], float] = {}
        for index, (left_id, left_vector) in enumerate(vectors):
            for right_id, right_vector in vectors[index + 1 :]:
                score = cosine_similarity(left_vector, right_vector)
                if score >= threshold:
                    union(parent, left_id, right_id)
                    similarities[(min(left_id, right_id), max(left_id, right_id))] = score

        grouped: dict[int, list[int]] = {}
        for face_id, _vector in vectors:
            grouped.setdefault(find(parent, face_id), []).append(face_id)
        groups = [sorted(members) for members in grouped.values() if len(members) >= 2]
        groups.sort(key=lambda members: (-len(members), members[0]))

        conn.execute("DELETE FROM face_group_members")
        conn.execute("DELETE FROM face_groups")
        conn.execute("DELETE FROM face_group_runs")
        run_id = int(
            conn.execute(
                "INSERT INTO face_group_runs(threshold, method) VALUES(?, 'cosine-threshold') RETURNING id",
                (threshold,),
            ).fetchone()["id"]
        )
        grouped_faces = 0
        for group_index, members in enumerate(groups, start=1):
            grouped_faces += len(members)
            group_id = int(
                conn.execute(
                    """
                    INSERT INTO face_groups(run_id, group_index, member_count)
                    VALUES(?, ?, ?)
                    RETURNING id
                    """,
                    (run_id, group_index, len(members)),
                ).fetchone()["id"]
            )
            for face_id in members:
                conn.execute(
                    """
                    INSERT INTO face_group_members(group_id, face_id, similarity)
                    VALUES(?, ?, ?)
                    """,
                    (group_id, face_id, best_group_similarity(face_id, members, similarities)),
                )
        conn.commit()
        return FaceGroupStats(
            faces=len(vectors),
            groups=len(groups),
            grouped_faces=grouped_faces,
            threshold=threshold,
        )
    finally:
        conn.close()


def export_face_groups_browser(target: Path, output: Path | None = None) -> Path:
    output_path = output or (target / "face-groups.html")
    path = face_db_path(target)
    if not path.exists():
        raise ValueError("Face-database finnes ikke. Kjør bildebank face-scan først.")
    conn = connect_face_db(target)
    try:
        items = face_group_browser_items(target, conn)
    finally:
        conn.close()
    output_path.write_text(render_face_groups_html(items), encoding="utf-8", newline="\n")
    return output_path


def face_group_browser_items(target: Path, conn: sqlite3.Connection) -> list[dict[str, Any]]:
    run = conn.execute("SELECT * FROM face_group_runs ORDER BY id DESC LIMIT 1").fetchone()
    if run is None:
        return []
    rows = conn.execute(
        """
        SELECT
            face_groups.group_index,
            face_groups.member_count,
            face_group_members.similarity,
            scanned_files.target_path,
            faces.bbox_x,
            faces.bbox_y,
            faces.bbox_width,
            faces.bbox_height,
            faces.detection_score
        FROM face_groups
        JOIN face_group_members ON face_group_members.group_id = face_groups.id
        JOIN faces ON faces.id = face_group_members.face_id
        JOIN scanned_files ON scanned_files.file_id = faces.file_id
        WHERE face_groups.run_id = ?
        ORDER BY face_groups.group_index, face_group_members.similarity DESC, faces.id
        """,
        (run["id"],),
    )
    groups: dict[int, dict[str, Any]] = {}
    for row in rows:
        group_index = int(row["group_index"])
        target_path = Path(str(row["target_path"]))
        dimensions = image_dimensions(target_path)
        face = {
            "path": display_relative_path(target, target_path),
            "url": path_to_url(relative_to_target(target, target_path)),
            "x": float(row["bbox_x"]),
            "y": float(row["bbox_y"]),
            "width": float(row["bbox_width"]),
            "height": float(row["bbox_height"]),
            "score": float(row["detection_score"]),
            "similarity": float(row["similarity"]),
            "dimensions": dimensions,
        }
        groups.setdefault(
            group_index,
            {
                "index": group_index,
                "memberCount": int(row["member_count"]),
                "personName": person_name_for_group(conn, group_index),
                "faces": [],
            },
        )["faces"].append(face)
    return list(groups.values())


def person_name_for_group(conn: sqlite3.Connection, group_index: int) -> str | None:
    group = latest_group_by_index(conn, group_index)
    if group is None:
        return None
    row = conn.execute(
        """
        SELECT persons.name
        FROM face_group_members
        JOIN person_faces ON person_faces.face_id = face_group_members.face_id
        JOIN persons ON persons.id = person_faces.person_id
        WHERE face_group_members.group_id = ?
        GROUP BY persons.id
        ORDER BY COUNT(*) DESC, persons.name
        LIMIT 1
        """,
        (int(group["id"]),),
    ).fetchone()
    return str(row["name"]) if row is not None else None


def face_browser_items(target: Path, conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            scanned_files.file_id,
            scanned_files.target_path,
            scanned_files.face_count,
            faces.bbox_x,
            faces.bbox_y,
            faces.bbox_width,
            faces.bbox_height,
            faces.detection_score,
            faces.embedding_model
        FROM scanned_files
        JOIN faces ON faces.file_id = scanned_files.file_id
        WHERE scanned_files.status = 'ok'
        ORDER BY scanned_files.target_path, faces.id
        """
    ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        target_path = Path(str(row["target_path"]))
        key = str(target_path)
        item = grouped.setdefault(
            key,
            {
                "path": display_relative_path(target, target_path),
                "url": path_to_url(relative_to_target(target, target_path)),
                "faceCount": int(row["face_count"]),
                "dimensions": image_dimensions(target_path),
                "faces": [],
            },
        )
        item["faces"].append(
            {
                "x": float(row["bbox_x"]),
                "y": float(row["bbox_y"]),
                "width": float(row["bbox_width"]),
                "height": float(row["bbox_height"]),
                "score": float(row["detection_score"]),
                "model": str(row["embedding_model"]),
            }
        )
    return list(grouped.values())


def relative_to_target(target: Path, path: Path) -> Path:
    try:
        return path.resolve().relative_to(target.resolve())
    except ValueError:
        import os

        return Path(os.path.relpath(path, target))


def display_relative_path(target: Path, path: Path) -> str:
    return relative_to_target(target, path).as_posix()


def path_to_url(path: Path) -> str:
    return "/".join(quote(part) for part in path.parts)


def render_face_browser_html(items: list[dict[str, Any]]) -> str:
    cards = "\n".join(render_face_card(item) for item in items)
    if not cards:
        cards = '<p class="empty">Ingen ansikter funnet ennå.</p>'
    return f"""<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ansikter</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f7f5;
      --text: #202020;
      --muted: #666;
      --border: #d8d8d2;
      --panel: #fff;
      --accent: #d62f2f;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header {{
      padding: 16px;
      border-bottom: 1px solid var(--border);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 2;
    }}
    h1 {{
      margin: 0;
      font-size: 20px;
    }}
    main {{
      padding: 16px;
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
      gap: 16px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
    }}
    .media {{
      position: relative;
      background: #eee;
    }}
    .media img {{
      display: block;
      width: 100%;
      height: auto;
    }}
    .box {{
      position: absolute;
      border: 2px solid var(--accent);
      background: rgb(214 47 47 / 12%);
      pointer-events: none;
    }}
    .meta {{
      padding: 10px;
      display: grid;
      gap: 4px;
      font-size: 13px;
    }}
    .path {{
      overflow-wrap: anywhere;
      font-weight: 600;
    }}
    .muted {{
      color: var(--muted);
    }}
    .empty {{
      grid-column: 1 / -1;
      margin: 0;
      color: var(--muted);
    }}
  </style>
</head>
<body>
  <header>
    <h1>Ansikter ({len(items)} bilder)</h1>
  </header>
  <main>
    {cards}
  </main>
</body>
</html>
"""


def render_face_groups_html(groups: list[dict[str, Any]]) -> str:
    sections = "\n".join(render_face_group_section(group) for group in groups)
    if not sections:
        sections = '<p class="empty">Ingen grupper beregnet ennå. Kjør bildebank face-group først.</p>'
    return f"""<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ansiktsgrupper</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f7f5;
      --text: #202020;
      --muted: #666;
      --border: #d8d8d2;
      --panel: #fff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header {{
      padding: 16px;
      border-bottom: 1px solid var(--border);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 2;
    }}
    h1 {{ margin: 0; font-size: 20px; }}
    main {{ padding: 16px; display: grid; gap: 18px; }}
    section {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
    }}
    h2 {{ margin: 0 0 10px; font-size: 16px; }}
    .faces {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
      gap: 10px;
    }}
    .face {{
      display: grid;
      gap: 5px;
      min-width: 0;
    }}
    .crop {{
      position: relative;
      aspect-ratio: 1;
      overflow: hidden;
      background: #eee;
      border: 1px solid var(--border);
      border-radius: 6px;
    }}
    .crop img {{
      position: absolute;
      height: auto;
      max-width: none;
    }}
    .meta {{
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }}
    .person, .command {{
      margin: 0 0 10px;
      color: var(--muted);
      font-size: 13px;
    }}
    .command {{
      font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
      background: #f0f0eb;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 7px;
      overflow-wrap: anywhere;
    }}
    .empty {{ margin: 0; color: var(--muted); }}
  </style>
</head>
<body>
  <header>
    <h1>Ansiktsgrupper ({len(groups)} grupper)</h1>
  </header>
  <main>
    {sections}
  </main>
</body>
</html>
"""


def render_face_group_section(group: dict[str, Any]) -> str:
    faces = "\n".join(render_group_face(face) for face in group["faces"])
    person = (
        f'<p class="person">Koblet til: {html_escape(group["personName"])}</p>'
        if group.get("personName")
        else '<p class="person">Forslag. Ikke bekreftet person.</p>'
    )
    return f"""<section>
  <h2>Gruppe {group['index']} ({group['memberCount']} ansikter)</h2>
  {person}
  <p class="command">bildebank face-person-add-group "Navn" {group['index']}</p>
  <div class="faces">
    {faces}
  </div>
</section>"""


def render_group_face(face: dict[str, Any]) -> str:
    style = crop_image_style(face)
    return f"""<div class="face">
  <div class="crop"><img src="{html_escape(face['url'])}" alt="" style="{style}"></div>
  <div class="meta">likhet {float(face['similarity']):.3f}<br>{html_escape(face['path'])}</div>
</div>"""


def crop_image_style(face: dict[str, Any]) -> str:
    crop = face_crop_percent(face, face["dimensions"])
    if crop is None:
        return "left: 0; top: 0; width: 100%;"
    left, top, width, _height = crop
    return (
        f"left: {-100.0 * left / width:.4f}%; "
        f"top: {-100.0 * top / width:.4f}%; "
        f"width: {10000.0 / width:.4f}%;"
    )


def render_face_card(item: dict[str, Any]) -> str:
    boxes = "\n".join(render_face_box(face, item["dimensions"]) for face in item["faces"])
    face_count = int(item["faceCount"])
    return f"""<article class="card">
  <div class="media">
    <img src="{html_escape(item['url'])}" alt="">
    {boxes}
  </div>
  <div class="meta">
    <div class="path">{html_escape(item['path'])}</div>
    <div>{face_count} ansikt{'er' if face_count != 1 else ''}</div>
    <div class="muted">Beste score: {max(float(face['score']) for face in item['faces']):.3f}</div>
  </div>
</article>"""


def render_face_box(face: dict[str, Any], dimensions) -> str:
    percent = face_box_percent(face, dimensions)
    if percent is None:
        left = top = width = height = 0.0
    else:
        left, top, width, height = percent
    return (
        '<div class="box" '
        f'title="score {float(face["score"]):.3f}" '
        'style="'
        f'left: {left:.4f}%; '
        f'top: {top:.4f}%; '
        f'width: {width:.4f}%; '
        f'height: {height:.4f}%;'
        '"></div>'
    )


def face_box_percent(face: dict[str, Any], dimensions) -> tuple[float, float, float, float] | None:
    if dimensions is None or dimensions.width <= 0 or dimensions.height <= 0:
        return None
    return (
        100.0 * float(face["x"]) / dimensions.width,
        100.0 * float(face["y"]) / dimensions.height,
        100.0 * float(face["width"]) / dimensions.width,
        100.0 * float(face["height"]) / dimensions.height,
    )


def face_crop_percent(face: dict[str, Any], dimensions) -> tuple[float, float, float, float] | None:
    box = face_box_percent(face, dimensions)
    if box is None:
        return None
    left, top, width, height = box
    size = max(width, height) * 2.2
    size = min(max(size, 12.0), 100.0)
    center_x = left + width / 2
    center_y = top + height / 2
    crop_left = min(max(center_x - size / 2, 0.0), max(100.0 - size, 0.0))
    crop_top = min(max(center_y - size / 2, 0.0), max(100.0 - size, 0.0))
    return crop_left, crop_top, size, size


def html_escape(value: object) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def count_scanned_files(conn: sqlite3.Connection, where_sql: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM scanned_files WHERE {where_sql}").fetchone()[0])


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
    main_conn = db.connect(target)
    face_conn = connect_face_db(target)
    try:
        rows_to_scan = []
        for row in active_image_files(main_conn, limit=limit):
            stats.checked += 1
            file_id = int(row["id"])
            sha256 = str(row["sha256"])
            if is_file_scanned(face_conn, file_id, sha256):
                stats.skipped += 1
                continue
            rows_to_scan.append(row)
        if not rows_to_scan:
            return stats

        app = load_face_app(config)
        for row in rows_to_scan:
            file_id = int(row["id"])
            target_path = Path(str(row["target_path"]))
            target_path_key = str(row["target_path_key"])
            sha256 = str(row["sha256"])
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


def embedding_from_blob(blob: bytes) -> list[float]:
    import array

    values = array.array("f")
    values.frombytes(blob)
    return list(values)


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def find(parent: dict[int, int], value: int) -> int:
    root = value
    while parent[root] != root:
        root = parent[root]
    while parent[value] != value:
        next_value = parent[value]
        parent[value] = root
        value = next_value
    return root


def union(parent: dict[int, int], left: int, right: int) -> None:
    left_root = find(parent, left)
    right_root = find(parent, right)
    if left_root != right_root:
        parent[max(left_root, right_root)] = min(left_root, right_root)


def best_group_similarity(face_id: int, members: list[int], similarities: dict[tuple[int, int], float]) -> float:
    scores = [
        similarities[(min(face_id, other_id), max(face_id, other_id))]
        for other_id in members
        if other_id != face_id and (min(face_id, other_id), max(face_id, other_id)) in similarities
    ]
    return max(scores) if scores else 1.0
