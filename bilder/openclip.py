from __future__ import annotations

import array
import html
import math
import os
import sqlite3
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import db
from .config import OpenClipConfig
from .media import IMAGE_EXTENSIONS, image_dimensions


OPENCLIP_DB_FILENAME = ".bilder-openclip.sqlite3"


@dataclass(frozen=True)
class OpenClipDbSummary:
    exists: bool
    embeddings: int
    search_runs: int
    search_results: int


@dataclass(frozen=True)
class ImageScanStats:
    total: int
    checked: int = 0
    to_scan: int = 0
    skipped: int = 0
    scanned: int = 0
    errors: int = 0
    last_error_path: Path | None = None
    last_error_message: str | None = None


@dataclass(frozen=True)
class ImageSearchResult:
    rank: int
    file_id: int
    target_path: Path
    similarity: float


@dataclass(frozen=True)
class ImageSearchStats:
    query: str
    run_id: int
    results: tuple[ImageSearchResult, ...]
    output_path: Path


@dataclass(frozen=True)
class ImageSearchProgressStats:
    query: str
    compared: int = 0
    total: int = 0


def openclip_db_path(target: Path) -> Path:
    return target / OPENCLIP_DB_FILENAME


def connect_openclip_db(target: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(openclip_db_path(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply_schema(conn)
    normalize_openclip_paths(conn, target)
    set_meta(conn, "target_path", str(target.resolve()))
    conn.commit()
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS image_embeddings (
            file_id INTEGER NOT NULL,
            target_path TEXT NOT NULL,
            target_path_key TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            model_name TEXT NOT NULL,
            pretrained TEXT NOT NULL,
            embedding BLOB NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(file_id, model_name, pretrained)
        );

        CREATE INDEX IF NOT EXISTS idx_image_embeddings_model
        ON image_embeddings(model_name, pretrained);

        CREATE TABLE IF NOT EXISTS image_search_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            model_name TEXT NOT NULL,
            pretrained TEXT NOT NULL,
            result_limit INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS image_search_results (
            run_id INTEGER NOT NULL REFERENCES image_search_runs(id) ON DELETE CASCADE,
            file_id INTEGER NOT NULL,
            target_path TEXT NOT NULL,
            target_path_key TEXT NOT NULL,
            similarity REAL NOT NULL,
            rank INTEGER NOT NULL,
            PRIMARY KEY(run_id, rank)
        );

        CREATE INDEX IF NOT EXISTS idx_image_search_results_run_id
        ON image_search_results(run_id);
        """
    )


def normalize_openclip_paths(conn: sqlite3.Connection, target: Path) -> None:
    current_root = target.resolve()
    stored_root_value = get_meta(conn, "target_path")
    old_root = Path(stored_root_value) if stored_root_value else current_root

    embedding_rows = conn.execute(
        "SELECT file_id, target_path FROM image_embeddings ORDER BY file_id, model_name, pretrained"
    ).fetchall()
    for row in embedding_rows:
        relative_path = db.target_relative_path(old_root, Path(str(row["target_path"])))
        conn.execute(
            """
            UPDATE image_embeddings
            SET target_path = ?, target_path_key = ?
            WHERE file_id = ? AND target_path = ?
            """,
            (
                relative_path.as_posix(),
                db.relative_path_key(relative_path),
                int(row["file_id"]),
                str(row["target_path"]),
            ),
        )

    search_rows = conn.execute(
        "SELECT run_id, file_id, target_path FROM image_search_results ORDER BY run_id, rank"
    ).fetchall()
    for row in search_rows:
        relative_path = db.target_relative_path(old_root, Path(str(row["target_path"])))
        conn.execute(
            """
            UPDATE image_search_results
            SET target_path = ?, target_path_key = ?
            WHERE run_id = ? AND file_id = ? AND target_path = ?
            """,
            (
                relative_path.as_posix(),
                db.relative_path_key(relative_path),
                int(row["run_id"]),
                int(row["file_id"]),
                str(row["target_path"]),
            ),
        )

    if stored_root_value is not None and stored_root_value != str(current_root):
        set_meta(conn, "target_path", str(current_root))


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def active_image_files(target: Path, *, limit: int | None = None) -> list[sqlite3.Row]:
    conn = db.connect(target)
    try:
        rows: list[sqlite3.Row] = []
        for row in conn.execute(
            """
            SELECT id, target_path, target_path_key, sha256, stored_filename
            FROM files
            WHERE deleted_at IS NULL
            ORDER BY imported_at, id
            """
        ):
            if Path(str(row["stored_filename"])).suffix.lower() in IMAGE_EXTENSIONS:
                rows.append(row)
                if limit is not None and len(rows) >= limit:
                    break
        return rows
    finally:
        conn.close()


ImageScanProgress = Callable[[str, int, int, ImageScanStats, Path | None], None]
ImageSearchProgress = Callable[[str, int, int, ImageSearchProgressStats], None]


def scan_images(
    target: Path,
    config: OpenClipConfig,
    *,
    limit: int | None = None,
    progress: ImageScanProgress | None = None,
) -> ImageScanStats:
    image_rows = active_image_files(target, limit=limit)
    total = len(image_rows)
    stats = ImageScanStats(total=total)
    if progress is not None:
        progress("start", 0, total, stats, None)
    if not image_rows:
        return stats

    conn = connect_openclip_db(target)
    try:
        rows_to_scan = []
        skipped = 0
        for index, row in enumerate(image_rows, start=1):
            file_id = int(row["id"])
            sha256 = str(row["sha256"])
            if has_current_embedding(conn, file_id, sha256, config):
                skipped += 1
            else:
                rows_to_scan.append(row)
            stats = ImageScanStats(
                total=total,
                checked=index,
                to_scan=len(rows_to_scan),
                skipped=skipped,
            )
            if progress is not None:
                progress("check", index, total, stats, db.absolute_target_path(target, Path(str(row["target_path"]))))
        if not rows_to_scan:
            if progress is not None:
                progress("done", total, total, stats, None)
            return stats
        if progress is not None:
            progress("load_model", 0, len(rows_to_scan), stats, None)
        model, preprocess = load_image_model(config)
        scanned = 0
        errors = 0
        last_error_path = None
        last_error_message = None
        for index, row in enumerate(rows_to_scan, start=1):
            file_id = int(row["id"])
            sha256 = str(row["sha256"])
            target_path = db.absolute_target_path(target, Path(str(row["target_path"])))
            try:
                vector = image_embedding(model, preprocess, target_path)
                store_embedding(
                    conn,
                    file_id=file_id,
                    target_root=target,
                    target_path=target_path,
                    target_path_key=str(row["target_path_key"]),
                    sha256=sha256,
                    config=config,
                    vector=vector,
                )
                scanned += 1
            except Exception as exc:
                errors += 1
                last_error_path = target_path
                last_error_message = str(exc)
                stats = ImageScanStats(
                    total=total,
                    checked=total,
                    to_scan=len(rows_to_scan),
                    skipped=skipped,
                    scanned=scanned,
                    errors=errors,
                    last_error_path=last_error_path,
                    last_error_message=last_error_message,
                )
                if progress is not None:
                    progress("error", index, len(rows_to_scan), stats, target_path)
                conn.commit()
                continue
            stats = ImageScanStats(
                total=total,
                checked=total,
                to_scan=len(rows_to_scan),
                skipped=skipped,
                scanned=scanned,
                errors=errors,
                last_error_path=last_error_path,
                last_error_message=last_error_message,
            )
            if progress is not None:
                progress("scan", index, len(rows_to_scan), stats, target_path)
            conn.commit()
        if progress is not None:
            progress("done", len(rows_to_scan), len(rows_to_scan), stats, None)
        return stats
    finally:
        conn.close()


def search_images(
    target: Path,
    config: OpenClipConfig,
    *,
    query: str,
    limit: int = 100,
    progress: ImageSearchProgress | None = None,
) -> ImageSearchStats:
    clean_query = query.strip()
    if not clean_query:
        raise ValueError("Søketekst kan ikke være tom.")
    if limit < 1:
        raise ValueError("--limit må være minst 1.")

    conn = connect_openclip_db(target)
    try:
        rows = list(
            conn.execute(
                """
                SELECT file_id, target_path, target_path_key, embedding
                FROM image_embeddings
                WHERE model_name = ? AND pretrained = ?
                """,
                (config.model_name, config.pretrained),
            )
        )
        if not rows:
            raise ValueError("Fant ingen bilde-embeddings. Kjør bildebank image-scan først.")
        search_progress = ImageSearchProgressStats(clean_query, compared=0, total=len(rows))
        if progress is not None:
            progress("load_model", 0, len(rows), search_progress)
        model, tokenizer = load_text_model(config)
        text_vector = text_embedding(model, tokenizer, clean_query)
        if progress is not None:
            progress("compare_start", 0, len(rows), search_progress)
        scored_items = []
        for index, row in enumerate(rows, start=1):
            scored_items.append(
                (
                    cosine_similarity(text_vector, embedding_from_blob(bytes(row["embedding"]))),
                    int(row["file_id"]),
                    db.absolute_target_path(target, Path(str(row["target_path"]))),
                    str(row["target_path_key"]),
                )
            )
            search_progress = ImageSearchProgressStats(clean_query, compared=index, total=len(rows))
            if progress is not None:
                progress("compare", index, len(rows), search_progress)
        scored = sorted(scored_items, reverse=True, key=lambda item: item[0])[:limit]
        if progress is not None:
            progress("write", len(scored), len(scored), search_progress)
        run_id = create_search_run(conn, clean_query, config, limit)
        results: list[ImageSearchResult] = []
        for index, (score, file_id, target_path, target_path_key) in enumerate(scored, start=1):
            conn.execute(
                """
                INSERT INTO image_search_results(run_id, file_id, target_path, target_path_key, similarity, rank)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (run_id, file_id, db.target_relative_path(target, target_path).as_posix(), target_path_key, score, index),
            )
            results.append(ImageSearchResult(index, file_id, target_path, score))
        conn.commit()
    finally:
        conn.close()

    output_path = export_image_search_html(target, clean_query, config, results)
    if progress is not None:
        progress("done", len(results), len(results), search_progress)
    return ImageSearchStats(clean_query, run_id, tuple(results), output_path)


def openclip_db_summary(target: Path) -> OpenClipDbSummary:
    path = openclip_db_path(target)
    if not path.exists():
        return OpenClipDbSummary(False, 0, 0, 0)
    conn = sqlite3.connect(path)
    try:
        return OpenClipDbSummary(
            exists=True,
            embeddings=count_rows_if_table_exists(conn, "image_embeddings"),
            search_runs=count_rows_if_table_exists(conn, "image_search_runs"),
            search_results=count_rows_if_table_exists(conn, "image_search_results"),
        )
    finally:
        conn.close()


def count_rows_if_table_exists(conn: sqlite3.Connection, table: str) -> int:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if row is None:
        return 0
    return count_rows(conn, table)


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def has_current_embedding(
    conn: sqlite3.Connection,
    file_id: int,
    sha256: str,
    config: OpenClipConfig,
) -> bool:
    row = conn.execute(
        """
        SELECT sha256
        FROM image_embeddings
        WHERE file_id = ? AND model_name = ? AND pretrained = ?
        """,
        (file_id, config.model_name, config.pretrained),
    ).fetchone()
    return row is not None and row["sha256"] == sha256


def store_embedding(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    target_root: Path,
    target_path: Path,
    target_path_key: str,
    sha256: str,
    config: OpenClipConfig,
    vector: list[float],
) -> None:
    conn.execute(
        """
        INSERT INTO image_embeddings(
            file_id, target_path, target_path_key, sha256, model_name, pretrained, embedding
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(file_id, model_name, pretrained) DO UPDATE SET
            target_path = excluded.target_path,
            target_path_key = excluded.target_path_key,
            sha256 = excluded.sha256,
            embedding = excluded.embedding,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            file_id,
            db.target_relative_path(target_root, target_path).as_posix(),
            target_path_key,
            sha256,
            config.model_name,
            config.pretrained,
            embedding_blob(vector),
        ),
    )


def create_search_run(
    conn: sqlite3.Connection,
    query: str,
    config: OpenClipConfig,
    limit: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO image_search_runs(query, model_name, pretrained, result_limit)
        VALUES(?, ?, ?, ?)
        """,
        (query, config.model_name, config.pretrained, limit),
    )
    return int(cur.lastrowid)


def load_image_model(config: OpenClipConfig) -> tuple[Any, Any]:
    open_clip = import_open_clip()
    device = resolve_torch_device(config.device)
    model, _, preprocess = open_clip.create_model_and_transforms(
        config.model_name,
        pretrained=config.pretrained,
        device=device,
        cache_dir=str(config.model_root),
    )
    model.eval()
    return model, preprocess


def load_text_model(config: OpenClipConfig) -> tuple[Any, Any]:
    open_clip = import_open_clip()
    device = resolve_torch_device(config.device)
    model, _, _ = open_clip.create_model_and_transforms(
        config.model_name,
        pretrained=config.pretrained,
        device=device,
        cache_dir=str(config.model_root),
    )
    model.eval()
    return model, open_clip.get_tokenizer(config.model_name)


def import_open_clip():
    try:
        import open_clip
    except ImportError as exc:
        raise ValueError("OpenCLIP er ikke installert. Kjør install-openclip.ps1 fra programmappen.") from exc
    return open_clip


def image_embedding(model: Any, preprocess: Any, path: Path) -> list[float]:
    try:
        from PIL import Image, ImageOps
        import torch
    except ImportError as exc:
        raise ValueError("OpenCLIP/Pillow mangler. Kjør install-openclip.ps1 fra programmappen.") from exc
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        tensor = preprocess(image).unsqueeze(0).to(model_device(model))
    with torch.no_grad():
        embedding = model.encode_image(tensor)
    return normalized_tensor_values(embedding)


def text_embedding(model: Any, tokenizer: Any, query: str) -> list[float]:
    try:
        import torch
    except ImportError as exc:
        raise ValueError("PyTorch mangler. Kjør install-openclip.ps1 fra programmappen.") from exc
    tokens = tokenizer([query]).to(model_device(model))
    with torch.no_grad():
        embedding = model.encode_text(tokens)
    return normalized_tensor_values(embedding)


def resolve_torch_device(configured_device: str = "auto") -> str:
    device = configured_device.strip().lower()
    if device == "auto":
        try:
            import torch
        except ImportError:
            return "cpu"
        return "cuda" if torch.cuda.is_available() else "cpu"
    if device in {"cpu", "cuda"}:
        return device
    raise ValueError("OpenCLIP device må være 'auto', 'cpu' eller 'cuda'.")


def model_device(model: Any) -> str:
    try:
        return str(next(model.parameters()).device)
    except StopIteration:
        return "cpu"


def torch_gpu_status() -> dict[str, str]:
    try:
        import torch
    except ImportError:
        return {"torch": "nei", "cuda": "nei", "device": "-"}
    cuda_available = torch.cuda.is_available()
    device_name = "-"
    if cuda_available:
        try:
            device_name = torch.cuda.get_device_name(0)
        except Exception:
            device_name = "cuda"
    return {
        "torch": "ja",
        "cuda": "ja" if cuda_available else "nei",
        "device": device_name,
    }


def normalized_tensor_values(tensor: Any) -> list[float]:
    tensor = tensor / tensor.norm(dim=-1, keepdim=True)
    values = tensor.squeeze(0).detach().cpu().tolist()
    return [float(value) for value in values]


def embedding_blob(vector: list[float]) -> bytes:
    return array.array("f", vector).tobytes()


def embedding_from_blob(blob: bytes) -> list[float]:
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


def export_image_search_html(
    target: Path,
    query: str,
    config: OpenClipConfig,
    results: list[ImageSearchResult],
) -> Path:
    output_path = target / "image-search.html"
    items = "\n".join(image_result_html(target, result) for result in results)
    output_path.write_text(
        f"""<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bildesøk: {html.escape(query)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #f7f7f7; color: #222; }}
    h1 {{ margin-bottom: 0.25rem; }}
    .meta {{ color: #555; margin-bottom: 1.5rem; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 1rem; }}
    .item {{ background: white; border: 1px solid #ddd; border-radius: 6px; overflow: hidden; }}
    .item img {{ width: 100%; aspect-ratio: 4 / 3; object-fit: cover; display: block; background: #eee; }}
    .text {{ padding: 0.75rem; font-size: 0.9rem; }}
    .path {{ overflow-wrap: anywhere; }}
    .score {{ color: #555; margin-top: 0.35rem; }}
  </style>
</head>
<body>
  <h1>Bildesøk: {html.escape(query)}</h1>
  <div class="meta">{len(results)} treff med {html.escape(config.model_name)} ({html.escape(config.pretrained)}). Sortert med beste match først.</div>
  <div class="grid">
{items}
  </div>
</body>
</html>
""",
        encoding="utf-8",
    )
    return output_path


def image_result_html(target: Path, result: ImageSearchResult) -> str:
    relative = relative_to_target(target, result.target_path)
    url = path_to_url(relative)
    dimensions = image_dimensions(db.absolute_target_path(target, result.target_path))
    size = f"{dimensions.width} x {dimensions.height}" if dimensions else "-"
    path_text = display_relative_path(target, result.target_path)
    return f"""    <div class="item">
      <a href="{html.escape(url)}"><img src="{html.escape(url)}" alt=""></a>
      <div class="text">
        <div class="path">#{result.rank} {html.escape(path_text)}</div>
        <div class="score">score={result.similarity:.3f} · {html.escape(size)}</div>
      </div>
    </div>"""


def relative_to_target(target: Path, path: Path) -> Path:
    candidate = db.absolute_target_path(target, Path(path))
    try:
        return candidate.resolve().relative_to(target.resolve())
    except ValueError:
        return Path(os.path.relpath(candidate, target))


def display_relative_path(target: Path, path: Path) -> str:
    return str(relative_to_target(target, path)).replace("\\", "/")


def path_to_url(path: Path) -> str:
    return urllib.parse.quote(str(path).replace("\\", "/"))
