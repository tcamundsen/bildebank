from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import db
from .exiftool import resolve_exiftool_path
from .progress import ProgressMeter


EXIFTOOL_DATE_TAGS = (
    "DateTimeOriginal",
    "CreateDate",
    "ModifyDate",
    "DateCreated",
    "MediaCreateDate",
    "TrackCreateDate",
    "GPSDateTime",
    "GPSDateStamp",
)


@dataclass(frozen=True)
class ExifToolMetadataGap:
    target_path: Path
    bdb_date: str
    bdb_source: str
    tag: str
    value: str
    date: str


def exiftool_metadata_gaps(
    target: Path,
    *,
    exiftool_path: Path | None = None,
    batch_size: int = 200,
    progress: bool = False,
    repo_root: Path | None = None,
) -> list[ExifToolMetadataGap]:
    tool = exiftool_path or resolve_exiftool_path(repo_root or Path(__file__).resolve().parents[1])
    if batch_size < 1:
        raise ValueError("batch_size må være minst 1")

    conn = db.connect(target)
    try:
        rows = list(db.non_metadata_files(conn))
    finally:
        conn.close()

    gaps: list[ExifToolMetadataGap] = []
    total = len(rows)
    progress_meter = ProgressMeter("exiftool", stream=sys.stderr)
    try:
        if progress:
            progress_meter.message(f"exiftool: {total} filer skal kontrolleres.")
        checked = 0
        for batch in batched(rows, batch_size):
            target_paths = [db.absolute_target_path(target, Path(str(row["target_path"]))) for row in batch]
            found_dates = exiftool_dates_batch(tool, target_paths)
            for row, target_path in zip(batch, target_paths, strict=True):
                found = found_dates.get(target_path)
                if found is not None:
                    tag, value, date = found
                    gaps.append(
                        ExifToolMetadataGap(
                            target_path=target_path,
                            bdb_date=row["taken_date"] or "-",
                            bdb_source=row["date_source"],
                            tag=tag,
                            value=value,
                            date=date,
                        )
                    )
                checked += 1
                if progress:
                    progress_meter.update(
                        checked,
                        total,
                        action="kontrollert",
                        details=f"funnet={len(gaps)}",
                        eta=True,
                    )
    finally:
        progress_meter.done()
    return gaps


def batched(items: list[Any], batch_size: int) -> list[list[Any]]:
    return [items[index : index + batch_size] for index in range(0, len(items), batch_size)]


def exiftool_dates_batch(exiftool_path: Path | str, paths: list[Path]) -> dict[Path, tuple[str, str, str]]:
    if not paths:
        return {}
    result = subprocess.run(
        [
            str(exiftool_path),
            "-j",
            *[f"-{tag}" for tag in EXIFTOOL_DATE_TAGS],
            *[str(path) for path in paths],
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"exiftool feilet: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("exiftool ga ugyldig JSON") from exc

    path_by_source = {str(path): path for path in paths}
    dates: dict[Path, tuple[str, str, str]] = {}
    for index, item in enumerate(payload if isinstance(payload, list) else []):
        source_file = item.get("SourceFile")
        path = path_by_source.get(str(source_file)) if source_file is not None else None
        if path is None and index < len(paths):
            path = paths[index]
        if path is None:
            continue
        found = first_date_from_exiftool_item(item)
        if found is not None:
            dates[path] = found
    return dates


def first_date_from_exiftool_item(item: dict[str, object]) -> tuple[str, str, str] | None:
    for tag in EXIFTOOL_DATE_TAGS:
        value = item.get(tag)
        if value is None:
            continue
        parsed = parse_exiftool_date(str(value))
        if parsed is not None:
            return tag, str(value), parsed
    return None


def parse_exiftool_date(value: str) -> str | None:
    match = re.search(
        r"(?P<y>19\d{2}|20\d{2})[:/-](?P<m>0[1-9]|1[0-2])[:/-](?P<d>0[1-9]|[12]\d|3[01])",
        value,
    )
    if match is None:
        return None
    return f"{match.group('y')}-{match.group('m')}-{match.group('d')}"
