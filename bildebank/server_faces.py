from __future__ import annotations

import html
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from . import db
from .config import FaceRecognitionConfig
from .face import face_db_path, normalize_person_name
from .html_export import browser_face_items_from_metadata, face_tables_exist
from .media import ImageDimensions, image_dimensions, image_orientation
from .server_browser import BrowserSource, items_by_file_ids, person_browser_source, person_item_url, person_url, rotation_style_attr


ShellPageRenderer = Callable[..., str]


def current_face_db_path(target: Path, face_config: FaceRecognitionConfig | None = None) -> Path:
    if face_config is None:
        face_config = FaceRecognitionConfig()
    return face_db_path(target, face_config)


def clear_face_caches() -> None:
    cached_confirmed_people_for_file.cache_clear()
    cached_person_file_ids.cache_clear()
    cached_registered_people.cache_clear()


def confirmed_people_for_file(
    target: Path,
    file_id: int,
    face_config: FaceRecognitionConfig | None = None,
) -> list[dict[str, object]]:
    db_path = current_face_db_path(target, face_config)
    try:
        mtime_ns = db_path.stat().st_mtime_ns
    except OSError:
        return []
    return [
        {"name": name, "url": person_item_url(name, file_id, show_faces=False), "confirmed": priority == 0}
        for name, priority in cached_confirmed_people_for_file(str(db_path), mtime_ns, file_id)
    ]


def registered_people(target: Path, face_config: FaceRecognitionConfig | None = None) -> list[dict[str, str]]:
    db_path = current_face_db_path(target, face_config)
    try:
        mtime_ns = db_path.stat().st_mtime_ns
    except OSError:
        return []
    return [
        {"name": name, "url": person_url(name)}
        for name in cached_registered_people(str(db_path), mtime_ns)
    ]


@lru_cache(maxsize=8)
def cached_registered_people(face_db_path: str, face_db_mtime_ns: int) -> tuple[str, ...]:
    conn = sqlite3.connect(face_db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not face_tables_exist(conn):
            return ()
        rows = conn.execute("SELECT name FROM persons ORDER BY name")
        return tuple(str(row["name"]) for row in rows)
    except sqlite3.Error:
        return ()
    finally:
        conn.close()


@lru_cache(maxsize=512)
def cached_confirmed_people_for_file(face_db_path: str, face_db_mtime_ns: int, file_id: int) -> tuple[tuple[str, int], ...]:
    conn = sqlite3.connect(face_db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not face_tables_exist(conn):
            return ()
        rows = conn.execute(
            """
            SELECT persons.name, 0 AS priority
            FROM person_faces
            JOIN persons ON persons.id = person_faces.person_id
            JOIN faces ON faces.id = person_faces.face_id
            WHERE faces.file_id = ?
            UNION ALL
            SELECT persons.name, 1 AS priority
            FROM face_suggestions
            JOIN persons ON persons.id = face_suggestions.person_id
            JOIN faces ON faces.id = face_suggestions.face_id
            WHERE faces.file_id = ?
            ORDER BY name, priority
            """,
            (file_id, file_id),
        )
        people: dict[str, int] = {}
        for row in rows:
            name = str(row["name"])
            priority = int(row["priority"])
            if name not in people or priority < people[name]:
                people[name] = priority
        return tuple(sorted(people.items()))
    except sqlite3.Error:
        return ()
    finally:
        conn.close()


def person_item_url_for_face(
    target: Path,
    person_name: str,
    face_id: int,
    face_config: FaceRecognitionConfig | None = None,
) -> str:
    db_path = current_face_db_path(target, face_config)
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT file_id FROM faces WHERE id = ?", (face_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        row = None
    if row is None:
        return person_url(person_name, show_faces=False)
    return person_item_url(person_name, int(row[0]), show_faces=False)


def active_file_id_set(target: Path, file_ids: list[int]) -> set[int]:
    unique_ids = sorted(set(file_ids))
    if not unique_ids:
        return set()
    placeholders = ",".join("?" for _ in unique_ids)
    conn = db.connect(target)
    try:
        rows = conn.execute(
            f"""
            SELECT id
            FROM files
            WHERE deleted_at IS NULL
              AND id IN ({placeholders})
            """,
            tuple(unique_ids),
        )
        return {int(row["id"]) for row in rows}
    finally:
        conn.close()


def unconfirmed_face_count_for_item(
    target: Path,
    file_id: int,
    face_config: FaceRecognitionConfig | None = None,
) -> int:
    db_path = current_face_db_path(target, face_config)
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not face_tables_exist(conn):
            return 0
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM faces
            WHERE faces.file_id = ?
              AND NOT EXISTS (
                SELECT 1
                FROM person_faces
                WHERE person_faces.face_id = faces.id
              )
            """,
            (file_id,),
        ).fetchone()
        return int(row[0] or 0) if row is not None else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def unconfirmed_faces_for_item(
    target: Path,
    item: Any,
    face_config: FaceRecognitionConfig | None = None,
) -> list[dict[str, object]]:
    db_path = current_face_db_path(target, face_config)
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not face_tables_exist(conn):
            return []
        rows = conn.execute(
            """
            SELECT
                faces.id,
                faces.bbox_x,
                faces.bbox_y,
                faces.bbox_width,
                faces.bbox_height,
                faces.detection_score
            FROM faces
            WHERE faces.file_id = ?
              AND NOT EXISTS (
                SELECT 1
                FROM person_faces
                WHERE person_faces.face_id = faces.id
              )
            ORDER BY faces.id
            """,
            (int(item["id"]),),
        )
        faces = [
            {
                "faceId": int(row["id"]),
                "score": float(row["detection_score"]),
                "x": float(row["bbox_x"]),
                "y": float(row["bbox_y"]),
                "width": float(row["bbox_width"]),
                "height": float(row["bbox_height"]),
            }
            for row in rows
        ]
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return cached_face_box_items_for_item(target, item, faces)


def person_by_name(
    target: Path,
    person_name: str,
    face_config: FaceRecognitionConfig | None = None,
) -> sqlite3.Row | None:
    db_path = current_face_db_path(target, face_config)
    if not db_path.exists():
        return None
    clean_name = normalize_person_name(person_name)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not face_tables_exist(conn):
            return None
        return conn.execute("SELECT id, name FROM persons WHERE name = ?", (clean_name,)).fetchone()
    finally:
        conn.close()


def person_file_ids(
    target: Path,
    person_name: str,
    *,
    include_suggestions: bool = True,
    face_config: FaceRecognitionConfig | None = None,
) -> list[int]:
    db_path = current_face_db_path(target, face_config)
    try:
        mtime_ns = db_path.stat().st_mtime_ns
    except OSError:
        return []
    clean_name = normalize_person_name(person_name)
    file_ids = list(cached_person_file_ids(str(db_path), mtime_ns, clean_name, include_suggestions))
    active_file_ids = active_file_id_set(target, file_ids)
    return [file_id for file_id in file_ids if file_id in active_file_ids]


@lru_cache(maxsize=256)
def cached_person_file_ids(
    face_db_path: str,
    face_db_mtime_ns: int,
    person_name: str,
    include_suggestions: bool,
) -> tuple[int, ...]:
    conn = sqlite3.connect(face_db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not face_tables_exist(conn):
            return ()
        person = conn.execute("SELECT id FROM persons WHERE name = ?", (person_name,)).fetchone()
        if person is None:
            return ()
        if include_suggestions:
            rows = conn.execute(
                """
                SELECT DISTINCT faces.file_id
                FROM person_faces
                JOIN faces ON faces.id = person_faces.face_id
                WHERE person_faces.person_id = ?
                UNION
                SELECT DISTINCT faces.file_id
                FROM face_suggestions
                JOIN faces ON faces.id = face_suggestions.face_id
                WHERE face_suggestions.person_id = ?
                ORDER BY file_id
                """,
                (int(person["id"]), int(person["id"])),
            )
        else:
            rows = conn.execute(
                """
                SELECT DISTINCT faces.file_id
                FROM person_faces
                JOIN faces ON faces.id = person_faces.face_id
                WHERE person_faces.person_id = ?
                ORDER BY faces.file_id
                """,
                (int(person["id"]),),
            )
        return tuple(int(row["file_id"]) for row in rows)
    finally:
        conn.close()


def person_items(
    target: Path,
    person_name: str,
    *,
    include_suggestions: bool = True,
    face_config: FaceRecognitionConfig | None = None,
) -> list[Any]:
    file_ids = person_file_ids(
        target,
        person_name,
        include_suggestions=include_suggestions,
        face_config=face_config,
    )
    return items_by_file_ids(target, file_ids)


def registered_people_rows(target: Path, face_config: FaceRecognitionConfig | None = None) -> list[dict[str, object]]:
    db_path = current_face_db_path(target, face_config)
    if not db_path.exists():
        return []
    face_conn = sqlite3.connect(db_path)
    face_conn.row_factory = sqlite3.Row
    try:
        if not face_tables_exist(face_conn):
            return []
        rows: list[dict[str, object]] = []
        for person in face_conn.execute("SELECT id, name FROM persons ORDER BY name"):
            person_id = int(person["id"])
            confirmed_file_ids = [
                int(row["file_id"])
                for row in face_conn.execute(
                    """
                    SELECT faces.file_id
                    FROM person_faces
                    JOIN faces ON faces.id = person_faces.face_id
                    WHERE person_faces.person_id = ?
                    """,
                    (person_id,),
                )
            ]
            suggested_file_ids = [
                int(row["file_id"])
                for row in face_conn.execute(
                    """
                    SELECT faces.file_id
                    FROM face_suggestions
                    JOIN faces ON faces.id = face_suggestions.face_id
                    WHERE face_suggestions.person_id = ?
                    """,
                    (person_id,),
                )
            ]
            active_file_ids = active_file_id_set(target, [*confirmed_file_ids, *suggested_file_ids])
            active_confirmed_file_ids = [file_id for file_id in confirmed_file_ids if file_id in active_file_ids]
            active_suggested_file_ids = [file_id for file_id in suggested_file_ids if file_id in active_file_ids]
            confirmed_counts_by_file: dict[int, int] = {}
            for file_id in active_confirmed_file_ids:
                confirmed_counts_by_file[file_id] = confirmed_counts_by_file.get(file_id, 0) + 1
            duplicate_counts = [count for count in confirmed_counts_by_file.values() if count > 1]
            rows.append(
                {
                    "name": str(person["name"]),
                    "confirmed_file_count": len(confirmed_counts_by_file),
                    "all_file_count": len(set(active_confirmed_file_ids) | set(active_suggested_file_ids)),
                    "suggestion_count": len(active_suggested_file_ids),
                    "duplicate_confirmed_file_count": len(duplicate_counts),
                    "max_confirmed_faces_per_file": max(duplicate_counts, default=0),
                }
            )
        return rows
    finally:
        face_conn.close()


def person_faces_for_item(
    target: Path,
    person_name: str,
    item: Any,
    *,
    include_suggestions: bool = True,
    face_config: FaceRecognitionConfig | None = None,
) -> list[dict[str, object]]:
    person = person_by_name(target, person_name, face_config)
    if person is None:
        return []
    conn = sqlite3.connect(current_face_db_path(target, face_config))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                'bekreftet' AS status,
                faces.id,
                1.0 AS similarity,
                faces.bbox_x,
                faces.bbox_y,
                faces.bbox_width,
                faces.bbox_height,
                faces.detection_score
            FROM person_faces
            JOIN faces ON faces.id = person_faces.face_id
            WHERE person_faces.person_id = ?
              AND faces.file_id = ?
            """ + (
                """
                UNION ALL
                SELECT
                    'forslag' AS status,
                    faces.id,
                    face_suggestions.similarity,
                    faces.bbox_x,
                    faces.bbox_y,
                    faces.bbox_width,
                    faces.bbox_height,
                    faces.detection_score
                FROM face_suggestions
                JOIN faces ON faces.id = face_suggestions.face_id
                WHERE face_suggestions.person_id = ?
                  AND faces.file_id = ?
                """
                if include_suggestions
                else ""
            ) + """
            ORDER BY status, id
            """,
            (
                (int(person["id"]), int(item["id"]), int(person["id"]), int(item["id"]))
                if include_suggestions
                else (int(person["id"]), int(item["id"]))
            ),
        )
        faces = [
            {
                "faceId": int(row["id"]),
                "status": str(row["status"]),
                "similarity": float(row["similarity"]),
                "score": float(row["detection_score"]),
                "x": float(row["bbox_x"]),
                "y": float(row["bbox_y"]),
                "width": float(row["bbox_width"]),
                "height": float(row["bbox_height"]),
            }
            for row in rows
        ]
    finally:
        conn.close()
    face_meta = {int(face["faceId"]): face for face in faces}
    rendered = cached_face_box_items_for_item(target, item, faces)
    for face in rendered:
        meta = face_meta.get(int(face["faceId"]))
        if meta is not None:
            face["status"] = meta["status"]
            face["similarity"] = meta["similarity"]
    return rendered


def source_duplicate_confirmed_faces_warning_html(
    target: Path,
    source: BrowserSource,
    item: Any,
    face_config: FaceRecognitionConfig | None = None,
) -> str:
    if source.person_name is None or source.include_suggestions:
        return ""
    count = confirmed_person_face_count_for_item(target, source.person_name, int(item["id"]), face_config)
    if count < 2:
        return ""
    return (
        '<div class="warning">'
        f"NB: {count} bekreftede ansikter for {html.escape(source.person_name)} i dette bildet"
        "</div>"
    )


def confirmed_person_face_count_for_item(
    target: Path,
    person_name: str,
    file_id: int,
    face_config: FaceRecognitionConfig | None = None,
) -> int:
    person = person_by_name(target, person_name, face_config)
    if person is None:
        return 0
    conn = sqlite3.connect(current_face_db_path(target, face_config))
    try:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM person_faces
            JOIN faces ON faces.id = person_faces.face_id
            WHERE person_faces.person_id = ?
              AND faces.file_id = ?
            """,
            (int(person["id"]), file_id),
        ).fetchone()
        return int(row[0]) if row is not None else 0
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def unconfirm_face_buttons_html(
    target: Path,
    source: BrowserSource,
    item: Any,
    face_config: FaceRecognitionConfig | None = None,
) -> str:
    if source.person_name is None or source.include_suggestions:
        return ""
    faces = person_faces_for_item(
        target,
        source.person_name,
        item,
        include_suggestions=False,
        face_config=face_config,
    )
    buttons = []
    for face in faces:
        face_id = int(face["faceId"])
        person_name = source.person_name
        buttons.append(
            '<button class="nav-button danger-button" type="button" '
            f'data-unconfirm-face="{face_id}" '
            f'data-unconfirm-person="{html.escape(person_name)}">'
            f"Avbekreft face-id {face_id}"
            "</button>"
        )
    return "\n".join(buttons)


def people_links_html(people: list[dict[str, object]]) -> str:
    if not people:
        return ""
    links = "\n".join(people_link_html(person) for person in people)
    return f'<div class="people">{links}</div>'


def people_link_html(person: dict[str, object]) -> str:
    name = str(person["name"])
    badge = '<span class="confirmed-badge" title="Bekreftet" aria-label="Bekreftet"> ✅</span>' if person.get("confirmed") else ""
    return (
        f'<a class="person-link" href="{html.escape(str(person["url"]))}" '
        f'data-person-name="{html.escape(name)}">{html.escape(name)}{badge}</a>'
    )


def faces_button_html(face_count: int, file_id: int) -> str:
    if face_count <= 0:
        return ""
    return f'<button class="faces-button" type="button" data-open-faces data-faces-item="{file_id}">Ubekreftet ansikter i bildet ({face_count})</button>'


def faces_overlay_html(item: Any) -> str:
    target_path = Path(str(item["target_path"]))
    return f"""
    <div id="faceOverlay" class="face-overlay" hidden>
      <div class="lightbox-bar">
        <div class="lightbox-title">Ansikter - {html.escape(target_path.name)}</div>
        <button class="lightbox-close" type="button" data-close-faces>Lukk</button>
      </div>
      <div class="lightbox-stage">
        <div class="face-list" data-face-list></div>
      </div>
    </div>
    """


def person_assignment_buttons_html(face_id: int, people: list[dict[str, str]]) -> str:
    if not people:
        return '<p class="empty">Ingen personer registrert.</p>'
    return "\n".join(
        (
            f'<button class="assign-person-button" type="button" '
            f'data-face-id="{face_id}" data-person-name="{html.escape(person["name"])}">'
            f'{html.escape(person["name"])}</button>'
        )
        for person in people
    )


def people_row_html(person: dict[str, object]) -> str:
    name = str(person["name"])
    confirmed_count = int(person["confirmed_file_count"])
    all_count = int(person["all_file_count"])
    suggestion_count = int(person["suggestion_count"])
    duplicate_count = int(person["duplicate_confirmed_file_count"])
    max_confirmed_faces = int(person["max_confirmed_faces_per_file"])
    confirmed_source = person_browser_source(name, include_suggestions=False, show_faces=False)
    all_source = person_browser_source(name, include_suggestions=True, show_faces=False)
    duplicate_warning = ""
    if duplicate_count > 0:
        duplicate_warning = (
            '<span class="warning people-warning">'
            f"NB: {max_confirmed_faces} bekreftede ansikter i samme bilde"
            "</span>"
        )
    return f"""
    <div class="people-row">
      <div class="people-name">
        <span>{html.escape(name)}</span>
        <button class="rename-person-link" type="button" data-open-person-rename data-person-name="{html.escape(name)}">endre navn</button>
        <button class="rename-person-link delete-person-link" type="button" data-delete-person-name="{html.escape(name)}">slett person</button>
      </div>
      {duplicate_warning}
      <a class="person-link" href="{html.escape(confirmed_source.root_url)}">Bekreftede bilder ({confirmed_count})</a>
      <a class="person-link" href="{html.escape(all_source.root_url)}">Bekreftede og forslag ({all_count})</a>
      <span class="status">forslag: {suggestion_count}</span>
    </div>
    """


def people_page_html(
    target: Path,
    face_config: FaceRecognitionConfig | None = None,
    *,
    shell_page_html: ShellPageRenderer,
    openclip_enabled: bool = True,
) -> str:
    people = registered_people_rows(target, face_config)
    rows = "\n".join(people_row_html(person) for person in people)
    content = (
        f'<div class="people-table">{rows}</div>'
        if rows
        else '<p class="meta">Ingen personer registrert.</p>'
    )
    return shell_page_html(
        "Personer",
        f"""
        <h1>Personer</h1>
        {content}
        {person_rename_dialog_html()}
        """,
        face_enabled=True,
        openclip_enabled=openclip_enabled,
    )


def person_rename_dialog_html() -> str:
    return """
    <div id="personRenameDialog" class="modal-overlay" hidden>
      <form class="modal-panel person-rename-form" data-person-rename-form>
        <h2>Endre navn</h2>
        <input type="hidden" name="old_name">
        <label for="personRenameName">Nytt navn</label>
        <input id="personRenameName" type="text" name="new_name" autocomplete="off" required>
        <p class="assign-status" data-person-rename-status></p>
        <div class="modal-actions">
          <button class="nav-button" type="submit">Lagre</button>
          <button class="nav-button" type="button" data-close-person-rename>Avbryt</button>
        </div>
      </form>
    </div>
    """


def person_item_media_html(item: Any, faces: list[dict[str, object]]) -> str:
    file_id = int(item["id"])
    target_path = Path(str(item["target_path"]))
    url = f"/file/{file_id}"
    name = html.escape(str(item["stored_filename"]))
    if target_path.suffix.lower().lstrip(".") in {"mp4", "mov", "m4v", "avi", "mpg", "mpeg", "mts", "m2ts", "3gp", "wmv"}:
        return f'<video src="{url}" controls></video>'
    boxes = "\n".join(person_face_box_html(face) for face in faces)
    return f"""
    <div class="person-media"{rotation_style_attr(item)}>
      <a href="{url}" target="_blank"><img src="{url}" alt="{name}"></a>
      {boxes}
    </div>
    """


def person_face_box_html(face: dict[str, object]) -> str:
    if not {"left", "top", "boxWidth", "boxHeight"} <= face.keys():
        return ""
    css_class = "person-face-box suggested" if face.get("status") == "forslag" else "person-face-box"
    title = f'{face.get("status", "")} face-id {face["faceId"]} score {float(face.get("similarity", 0.0)):.3f}'
    label = f'face-id {face["faceId"]}'
    return (
        f'<div class="{css_class}" title="{html.escape(title)}" style="'
        f'left: {float(face["left"]):.4f}%; '
        f'top: {float(face["top"]):.4f}%; '
        f'width: {float(face["boxWidth"]):.4f}%; '
        f'height: {float(face["boxHeight"]):.4f}%;'
        f'"><span class="person-face-label">{html.escape(label)}</span></div>'
    )


def face_overlay_content_html(
    target: Path,
    item: Any,
    face_config: FaceRecognitionConfig | None = None,
) -> str:
    faces = unconfirmed_faces_for_item(target, item, face_config)
    if not faces:
        return '<p class="empty">Ingen ubekreftede ansikter i bildet.</p>'
    people = registered_people(target, face_config)
    image_url = f"/file/{int(item['id'])}"
    return "\n".join(face_overlay_item_html(item, image_url, face, people) for face in faces)


def face_overlay_item_html(item: Any, image_url: str, face: dict[str, object], people: list[dict[str, str]]) -> str:
    face_id = int(face["faceId"])
    people_buttons = person_assignment_buttons_html(face_id, people)
    box = ""
    if {"left", "top", "boxWidth", "boxHeight"} <= face.keys():
        box = (
            '<div class="face-box" style="'
            f'left: {float(face["left"]):.4f}%; '
            f'top: {float(face["top"]):.4f}%; '
            f'width: {float(face["boxWidth"]):.4f}%; '
            f'height: {float(face["boxHeight"]):.4f}%;'
            '"></div>'
        )
    return f"""
    <section class="face-detail" data-face-detail="{face_id}">
      <div class="face-detail-title">face-id {face_id}, deteksjon {float(face["score"]):.3f}</div>
      <div class="lightbox-media"{rotation_style_attr(item)}>
        <img src="{html.escape(image_url)}" alt="">
        {box}
      </div>
      <div class="assign-row">{people_buttons}</div>
      <form class="new-person-form" data-new-person-form>
        <input type="hidden" name="face_id" value="{face_id}">
        <label for="new-person-{face_id}">Ny person</label>
        <input id="new-person-{face_id}" name="person_name" autocomplete="off">
        <button type="submit">Identifiser</button>
      </form>
      <div class="assign-status" aria-live="polite"></div>
    </section>
    """


def cached_face_box_items_for_item(target: Path, item: Any, faces: list[dict[str, object]]) -> list[dict[str, object]]:
    dimensions, orientation = cached_face_box_media_metadata(target, item)
    return browser_face_items_from_metadata(faces, dimensions, orientation)


def cached_face_box_media_metadata(target: Path, item: Any) -> tuple[ImageDimensions | None, int]:
    target_path = db.absolute_target_path(target, Path(str(item["target_path"])))
    mtime_ns = file_mtime_ns(target_path)
    cached_dimensions, cached_orientation = face_box_media_metadata_from_item(item, mtime_ns)
    if cached_orientation is not None:
        return cached_dimensions, cached_orientation

    dimensions = image_dimensions(target_path)
    orientation = image_orientation(target_path)
    update_face_box_media_metadata(target, int(item["id"]), dimensions, orientation, mtime_ns)
    return dimensions, orientation


def face_box_media_metadata_from_item(item: Any, mtime_ns: int | None) -> tuple[ImageDimensions | None, int | None]:
    cached_mtime = item_field(item, "media_metadata_mtime_ns")
    if cached_mtime is None or mtime_ns is None or int(cached_mtime) != mtime_ns:
        return None, None
    orientation = item_field(item, "media_orientation")
    if orientation is None:
        return None, None
    width = item_field(item, "media_width")
    height = item_field(item, "media_height")
    dimensions = None
    if width is not None and height is not None and int(width) > 0 and int(height) > 0:
        dimensions = ImageDimensions(int(width), int(height))
    return dimensions, int(orientation)


def item_field(item: Any, key: str) -> Any | None:
    try:
        return item[key]
    except (KeyError, IndexError):
        return None


def file_mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def update_face_box_media_metadata(
    target: Path,
    file_id: int,
    dimensions: ImageDimensions | None,
    orientation: int,
    mtime_ns: int | None,
) -> None:
    conn = db.connect(target)
    try:
        conn.execute(
            """
            UPDATE files
            SET media_width = ?,
                media_height = ?,
                media_orientation = ?,
                media_metadata_mtime_ns = ?
            WHERE id = ?
            """,
            (
                dimensions.width if dimensions is not None else None,
                dimensions.height if dimensions is not None else None,
                orientation,
                mtime_ns,
                file_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
