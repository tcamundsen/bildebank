from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import db
from .exiftool import resolve_exiftool_path
from .progress import ProgressMeter
from .target_lock import TargetLock
from .value_parsing import optional_float, require_float


GPS_TAGS = (
    "GPSLatitude",
    "GPSLongitude",
    "GPSAltitude",
)
H3_COLUMNS = {resolution: f"h3_res{resolution}" for resolution in range(12)}
H3_AREA_LABELS_KM2 = {
    0: "ca. 4 357 450 km²",
    1: "ca. 609 790 km²",
    2: "ca. 86 800 km²",
    3: "ca. 12 390 km²",
    4: "ca. 1 770 km²",
    5: "ca. 250 km²",
    6: "ca. 36 km²",
    7: "ca. 5 km²",
    8: "ca. 0,7 km²",
    9: "ca. 0,1 km²",
    10: "ca. 15000 m²",
    11: "ca. 2000 m²",
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


@dataclass(frozen=True)
class PredefinedGeoPlace:
    slug: str
    name: str
    h3_cells: tuple[str, ...]


PREDEFINED_GEO_PLACES: tuple[PredefinedGeoPlace, ...] = (
    PredefinedGeoPlace(
        slug="kreta",
        name="Kreta",
        h3_cells=(
            "833f72fffffffff",
            "833f73fffffffff",
            "843f729ffffffff",
            "843f72bffffffff",
            "843f731ffffffff",
            "843f73dffffffff",
        ),
    ),
    PredefinedGeoPlace(
        slug="hadseløya",
        name="Hadseløya",
        h3_cells=(
            "86095876fffffff",
            "860958397ffffff",
            "86095839fffffff",
            "8609582b7ffffff",
            "8609582a7ffffff",
            "86095874fffffff",
            "860958747ffffff",
            "860958297ffffff",
            "860958287ffffff",
        ),
    ),
    PredefinedGeoPlace(
        slug="byske",
        name="Byske",
        h3_cells=(
            "8608f1417ffffff",
        ),
    ),
)


def predefined_geo_place(slug: str) -> PredefinedGeoPlace | None:
    clean_slug = slug.strip().lower()
    return next((place for place in PREDEFINED_GEO_PLACES if place.slug == clean_slug), None)


def extract_gps_from_metadata(meta: dict[str, object]) -> GpsData | None:
    lat = first_metadata_value(meta, "EXIF:GPSLatitude", "Composite:GPSLatitude", "GPSLatitude")
    lon = first_metadata_value(meta, "EXIF:GPSLongitude", "Composite:GPSLongitude", "GPSLongitude")
    alt = first_metadata_value(meta, "EXIF:GPSAltitude", "Composite:GPSAltitude", "GPSAltitude")
    if lat is None or lon is None:
        return None

    try:
        lat_value = require_float(lat, "GPS-breddegrad")
        lon_value = require_float(lon, "GPS-lengdegrad")
        alt_value = optional_float(alt, "GPS-høyde")
    except ValueError as exc:
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


def h3_cells_for_point(lat: float, lon: float) -> dict[str, str | None]:
    import h3

    return {column: h3.latlng_to_cell(lat, lon, resolution) for resolution, column in H3_COLUMNS.items()}


def h3_cells_for_manual_cell(h3_cell: str) -> dict[str, str | None]:
    """Return H3 columns for a manually selected cell.

    A manual H3 cell means "the image was taken somewhere inside this
    cell".  It does not mean that the image was taken at the center of
    the cell, so descendant cells at higher resolutions must remain
    unknown.
    """
    import h3

    resolution = h3_resolution(h3_cell)
    cells: dict[str, str | None] = {}
    for candidate_resolution, column in H3_COLUMNS.items():
        if candidate_resolution < resolution:
            cells[column] = h3.cell_to_parent(h3_cell, candidate_resolution)
        elif candidate_resolution == resolution:
            cells[column] = h3_cell
        else:
            cells[column] = None
    return cells


def h3_column_for_resolution(resolution: int) -> str:
    try:
        return H3_COLUMNS[resolution]
    except KeyError as exc:
        raise ValueError("H3-oppløsning må være mellom 0 og 11.") from exc


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


def default_exiftool_path(repo_root: Path | None = None) -> Path | str:
    return resolve_exiftool_path(repo_root or Path(__file__).resolve().parents[1])


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
    only_missing: bool = True,
    override_manual_h3: bool = False,
    limit: int | None = None,
    verbose: bool = False,
    exiftool_path: Path | str | None = None,
    batch_size: int = DEFAULT_EXIFTOOL_BATCH_SIZE,
    repo_root: Path | None = None,
) -> GeoScanStats:
    with TargetLock(target, command="geo-scan"):
        return _scan_geo_unlocked(
            target,
            force=force,
            only_missing=only_missing,
            override_manual_h3=override_manual_h3,
            limit=limit,
            verbose=verbose,
            exiftool_path=exiftool_path,
            batch_size=batch_size,
            repo_root=repo_root,
        )


def _scan_geo_unlocked(
    target: Path,
    *,
    force: bool = False,
    only_missing: bool = True,
    override_manual_h3: bool = False,
    limit: int | None = None,
    verbose: bool = False,
    exiftool_path: Path | str | None = None,
    batch_size: int = DEFAULT_EXIFTOOL_BATCH_SIZE,
    repo_root: Path | None = None,
) -> GeoScanStats:
    tool = exiftool_path or default_exiftool_path(repo_root)
    progress = ProgressMeter("geo-scan", stream=sys.stderr)
    conn = db.connect(target)
    checked = 0
    with_gps = 0
    without_gps = 0
    errors = 0
    updated = 0
    try:
        rows = db.geo_scan_files(
            conn,
            force=force,
            only_missing=only_missing and not (force or override_manual_h3),
            override_manual_h3=override_manual_h3,
            limit=limit,
        )
        file_ids_by_path: dict[Path, int] = {}
        paths: list[Path] = []
        for row in rows:
            path = db.absolute_target_path(target, Path(str(row["target_path"])))
            file_ids_by_path[path] = int(row["id"])
            paths.append(path)

        total = len(paths)
        if total:
            progress.message(f"geo-scan: {total} filer skal kontrolleres.")
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
                        gps_error=db.GPS_ERROR_FILE_MISSING,
                    )
                    if verbose:
                        print(f"Mangler fil: {path}")
                    continue
                existing_batch.append(path)

            if existing_batch:
                try:
                    metadata_by_path = read_gps_metadata_batch(tool, existing_batch)
                except Exception as exc:  # noqa: BLE001 - one batch should not stop the scan
                    errors += len(existing_batch)
                    if verbose:
                        print(f"ExifTool-feil for batch med {len(existing_batch)} filer: {exc}")
                    continue
                else:
                    for path in existing_batch:
                        meta = metadata_by_path.get(path)
                        if meta is None:
                            meta = metadata_by_path.get(Path(str(path)))
                        error_value = meta.get("Error") if meta else None
                        try:
                            gps = None if meta is None else extract_gps_from_metadata(meta)
                            cells: dict[str, str | None] | None = (
                                None if gps is None else h3_cells_for_point(gps.lat, gps.lon)
                            )
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
                                gps_error=db.GPS_ERROR_EXIFTOOL,
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
                                gps_error=db.GPS_ERROR_EXIFTOOL,
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
                progress.update(
                    min(checked, total),
                    total,
                    action="kontrollert",
                    details=f"med_gps={with_gps}, uten_gps={without_gps}, feil={errors}",
                    eta=True,
                )
    finally:
        progress.done()
        conn.close()

    return GeoScanStats(
        checked=checked,
        with_gps=with_gps,
        without_gps=without_gps,
        errors=errors,
        updated=updated,
    )
