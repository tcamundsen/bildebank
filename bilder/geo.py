from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import db
from .progress import ProgressLine


GPS_TAGS = (
    "GPSLatitude",
    "GPSLongitude",
    "GPSAltitude",
)
H3_COLUMNS = {
    5: "h3_res5",
    6: "h3_res6",
    7: "h3_res7",
    8: "h3_res8",
    9: "h3_res9",
}
H3_AREA_LABELS_KM2 = {
    5: "ca. 250 km²",
    6: "ca. 36 km²",
    7: "ca. 5 km²",
    8: "ca. 0,7 km²",
    9: "ca. 0,1 km²",
}
DEFAULT_EXIFTOOL_BATCH_SIZE = 200


@dataclass(frozen=True)
class GpsData:
    lat: float
    lon: float
    alt: float | None = None


@dataclass(frozen=True)
class GeoScanStats:
    checked: int = 0
    with_gps: int = 0
    without_gps: int = 0
    errors: int = 0
    updated: int = 0


def extract_gps_from_metadata(meta: dict[str, object]) -> GpsData | None:
    lat = first_metadata_value(meta, "EXIF:GPSLatitude", "Composite:GPSLatitude", "GPSLatitude")
    lon = first_metadata_value(meta, "EXIF:GPSLongitude", "Composite:GPSLongitude", "GPSLongitude")
    alt = first_metadata_value(meta, "EXIF:GPSAltitude", "Composite:GPSAltitude", "GPSAltitude")
    if lat is None or lon is None:
        return None

    try:
        lat_value = float(lat)
        lon_value = float(lon)
        alt_value = float(alt) if alt is not None else None
    except (TypeError, ValueError) as exc:
        raise ValueError("Ugyldige GPS-koordinater") from exc

    if not -90 <= lat_value <= 90:
        raise ValueError(f"Invalid GPS latitude: {lat_value}")
    if not -180 <= lon_value <= 180:
        raise ValueError(f"Invalid GPS longitude: {lon_value}")
    return GpsData(lat=lat_value, lon=lon_value, alt=alt_value)


def first_metadata_value(meta: dict[str, object], *keys: str) -> object | None:
    for key in keys:
        value = meta.get(key)
        if value is not None:
            return value
    return None


def h3_cells_for_point(lat: float, lon: float) -> dict[str, str]:
    import h3

    return {column: h3.latlng_to_cell(lat, lon, resolution) for resolution, column in H3_COLUMNS.items()}


def h3_column_for_resolution(resolution: int) -> str:
    try:
        return H3_COLUMNS[resolution]
    except KeyError as exc:
        raise ValueError("H3-oppløsning må være 5, 6, 7, 8 eller 9.") from exc


def h3_area_label(resolution: int) -> str:
    h3_column_for_resolution(resolution)
    return H3_AREA_LABELS_KM2[resolution]


def h3_resolution_label(resolution: int) -> str:
    return f"oppløsning {resolution}, {h3_area_label(resolution)}"


def h3_resolution(h3_cell: str) -> int:
    import h3

    if hasattr(h3, "is_valid_cell") and not h3.is_valid_cell(h3_cell):
        raise ValueError(f"Ugyldig H3-celle: {h3_cell}")
    try:
        resolution = int(h3.get_resolution(h3_cell))
    except Exception as exc:  # noqa: BLE001 - h3 raises library-specific exceptions
        raise ValueError(f"Ugyldig H3-celle: {h3_cell}") from exc
    h3_column_for_resolution(resolution)
    return resolution


def batched(items: list[Path], batch_size: int) -> Iterable[list[Path]]:
    if batch_size < 1:
        raise ValueError("batch_size må være minst 1")
    for index in range(0, len(items), batch_size):
        yield items[index : index + batch_size]


def default_exiftool_path(target: Path) -> Path | str:
    bundled = target / "exiftool.exe"
    return bundled if bundled.exists() else "exiftool"


def read_gps_metadata_batch(exiftool_path: Path | str, paths: list[Path]) -> dict[Path, dict[str, object]]:
    if not paths:
        return {}
    result = subprocess.run(
        [
            str(exiftool_path),
            "-json",
            "-n",
            *[f"-{tag}" for tag in GPS_TAGS],
            *[str(path) for path in paths],
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if not result.stdout.strip():
        message = result.stderr.strip() or f"exiftool feilet med exitkode {result.returncode}"
        raise RuntimeError(message)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("exiftool ga ugyldig JSON") from exc
    if not isinstance(payload, list):
        raise RuntimeError("exiftool ga uventet JSON-format")

    by_path: dict[Path, dict[str, object]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        source = item.get("SourceFile")
        if source is None:
            continue
        by_path[Path(str(source))] = item
    return by_path


def scan_geo(
    target: Path,
    *,
    force: bool = False,
    only_missing: bool = False,
    limit: int | None = None,
    verbose: bool = False,
    exiftool_path: Path | str | None = None,
    batch_size: int = DEFAULT_EXIFTOOL_BATCH_SIZE,
) -> GeoScanStats:
    tool = exiftool_path or default_exiftool_path(target)
    progress_line = ProgressLine(sys.stderr)
    conn = db.connect(target)
    checked = 0
    with_gps = 0
    without_gps = 0
    errors = 0
    updated = 0
    try:
        rows = db.geo_scan_files(conn, force=force, only_missing=only_missing, limit=limit)
        file_ids_by_path: dict[Path, int] = {}
        paths: list[Path] = []
        for row in rows:
            path = db.absolute_target_path(target, Path(str(row["target_path"])))
            file_ids_by_path[path] = int(row["id"])
            paths.append(path)

        total = len(paths)
        for batch in batched(paths, batch_size):
            existing_batch: list[Path] = []
            for path in batch:
                checked += 1
                if not path.exists():
                    errors += 1
                    updated += 1
                    db.update_file_gps(
                        conn,
                        file_id=file_ids_by_path[path],
                        gps_lat=None,
                        gps_lon=None,
                        gps_alt=None,
                        h3_cells=None,
                        gps_source="exiftool",
                        gps_error=f"Filen finnes ikke: {path}",
                    )
                    if verbose:
                        print(f"Mangler fil: {path}")
                    continue
                existing_batch.append(path)

            if existing_batch:
                try:
                    metadata_by_path = read_gps_metadata_batch(tool, existing_batch)
                except Exception as exc:  # noqa: BLE001 - one batch should not stop the scan
                    message = str(exc)
                    for path in existing_batch:
                        errors += 1
                        updated += 1
                        db.update_file_gps(
                            conn,
                            file_id=file_ids_by_path[path],
                            gps_lat=None,
                            gps_lon=None,
                            gps_alt=None,
                            h3_cells=None,
                            gps_source="exiftool",
                            gps_error=message,
                        )
                        if verbose:
                            print(f"Feil: {path}: {message}")
                else:
                    for path in existing_batch:
                        meta = metadata_by_path.get(path)
                        if meta is None:
                            meta = metadata_by_path.get(Path(str(path)))
                        error_value = meta.get("Error") if meta else None
                        try:
                            gps = None if meta is None else extract_gps_from_metadata(meta)
                            cells = None if gps is None else h3_cells_for_point(gps.lat, gps.lon)
                        except Exception as exc:  # noqa: BLE001 - bad metadata should be recorded per file
                            errors += 1
                            updated += 1
                            db.update_file_gps(
                                conn,
                                file_id=file_ids_by_path[path],
                                gps_lat=None,
                                gps_lon=None,
                                gps_alt=None,
                                h3_cells=None,
                                gps_source="exiftool",
                                gps_error=str(exc),
                            )
                            if verbose:
                                print(f"Feil: {path}: {exc}")
                            continue

                        if error_value is not None:
                            errors += 1
                            updated += 1
                            db.update_file_gps(
                                conn,
                                file_id=file_ids_by_path[path],
                                gps_lat=None,
                                gps_lon=None,
                                gps_alt=None,
                                h3_cells=None,
                                gps_source="exiftool",
                                gps_error=str(error_value),
                            )
                        elif gps is None:
                            without_gps += 1
                            updated += 1
                            db.update_file_gps(
                                conn,
                                file_id=file_ids_by_path[path],
                                gps_lat=None,
                                gps_lon=None,
                                gps_alt=None,
                                h3_cells=None,
                                gps_source="exiftool",
                                gps_error=None,
                            )
                            if verbose:
                                print(f"Ingen GPS: {path}")
                        else:
                            with_gps += 1
                            updated += 1
                            db.update_file_gps(
                                conn,
                                file_id=file_ids_by_path[path],
                                gps_lat=gps.lat,
                                gps_lon=gps.lon,
                                gps_alt=gps.alt,
                                h3_cells=cells,
                                gps_source="exiftool",
                                gps_error=None,
                            )

            conn.commit()
            if total:
                progress_line.write(f"geo-scan {min(checked, total)}/{total}")
    finally:
        progress_line.finish()
        conn.close()

    return GeoScanStats(
        checked=checked,
        with_gps=with_gps,
        without_gps=without_gps,
        errors=errors,
        updated=updated,
    )
