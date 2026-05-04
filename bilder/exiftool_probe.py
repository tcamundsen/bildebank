from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import db
from .progress import ProgressLine


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
    target: Path, *, exiftool_path: Path | None = None, progress: bool = False
) -> list[ExifToolMetadataGap]:
    tool = exiftool_path or (target / "exiftool.exe")
    if not tool.exists():
        raise FileNotFoundError(f"Fant ikke exiftool: {tool}")

    conn = db.connect(target)
    try:
        rows = list(db.non_metadata_files(conn))
    finally:
        conn.close()

    gaps: list[ExifToolMetadataGap] = []
    total = len(rows)
    progress_line = ProgressLine(sys.stderr)
    try:
        for index, row in enumerate(rows, start=1):
            if progress and (index == 1 or index == total or index % 25 == 0):
                progress_line.write(f"exiftool {index}/{total}: {row['target_path']}")
            target_path = Path(str(row["target_path"]))
            found = exiftool_first_date(tool, target_path)
            if found is None:
                continue
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
    finally:
        progress_line.finish()
    return gaps


def exiftool_first_date(exiftool_path: Path, path: Path) -> tuple[str, str, str] | None:
    result = subprocess.run(
        [
            str(exiftool_path),
            "-j",
            *[f"-{tag}" for tag in EXIFTOOL_DATE_TAGS],
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"exiftool feilet for {path}: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"exiftool ga ugyldig JSON for {path}") from exc
    if not payload:
        return None

    item = payload[0]
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
