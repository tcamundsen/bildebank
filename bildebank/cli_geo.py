from __future__ import annotations

import argparse
from pathlib import Path

from . import db
from .geo import h3_column_for_resolution, h3_resolution, scan_geo


def run_geo_command(args: argparse.Namespace, target: Path, *, repo_root: Path) -> int:
    if args.command == "geo-scan":
        return run_geo_scan(
            target,
            repo_root=repo_root,
            force=args.force,
            only_missing=args.only_missing or not (args.force or args.retry_missing or args.override_manual_h3),
            retry_missing=args.retry_missing,
            override_manual_h3=args.override_manual_h3,
            limit=args.limit,
            verbose=args.verbose,
            exiftool_path=args.exiftool,
            batch_size=args.batch_size,
        )

    if args.command == "geo-stats":
        return run_geo_stats(target)

    if args.command == "geo-areas":
        return run_geo_areas(target, resolution=args.resolution, min_count=args.min_count, limit=args.limit)

    return run_geo_area(
        target,
        h3_cell=args.h3_cell,
        limit=args.limit,
        with_date=args.with_date,
        with_coordinates=args.with_coordinates,
    )


def run_geo_scan(
    target: Path,
    *,
    repo_root: Path,
    force: bool,
    only_missing: bool,
    retry_missing: bool,
    override_manual_h3: bool,
    limit: int | None,
    verbose: bool,
    exiftool_path: Path | None,
    batch_size: int,
) -> int:
    if force and only_missing:
        raise ValueError("--force og --only-missing kan ikke brukes samtidig.")
    if force and retry_missing:
        raise ValueError("--force og --retry-missing kan ikke brukes samtidig.")
    if retry_missing and only_missing:
        raise ValueError("--retry-missing og --only-missing kan ikke brukes samtidig.")
    if override_manual_h3 and retry_missing:
        raise ValueError("--override-manual-h3 og --retry-missing kan ikke brukes samtidig.")
    if override_manual_h3 and only_missing:
        raise ValueError("--override-manual-h3 og --only-missing kan ikke brukes samtidig.")
    stats = scan_geo(
        target,
        force=force,
        only_missing=only_missing,
        override_manual_h3=override_manual_h3,
        limit=limit,
        verbose=verbose,
        exiftool_path=exiftool_path.resolve() if exiftool_path else None,
        batch_size=batch_size,
        repo_root=repo_root,
    )
    print("Scanning GPS metadata...")
    print(f"Images checked: {stats.checked}")
    print(f"With GPS:        {stats.with_gps}")
    print(f"Without GPS:     {stats.without_gps}")
    print(f"Errors:          {stats.errors}")
    print(f"Updated:         {stats.updated}")
    return 0 if stats.errors == 0 else 2


def run_geo_stats(target: Path) -> int:
    conn = db.connect(target)
    try:
        stats = db.geo_stats(conn)
    finally:
        conn.close()
    print(f"Images total:             {stats['total']}")
    print(f"Images scanned for GPS:   {stats['scanned']}")
    print(f"Images with GPS:          {stats['with_gps']}")
    print(f"Images without GPS:       {stats['without_gps']}")
    print(f"Images with GPS errors:   {stats['errors']}")
    return 0


def run_geo_areas(target: Path, *, resolution: int, min_count: int, limit: int) -> int:
    column = h3_column_for_resolution(resolution)
    conn = db.connect(target)
    try:
        rows = db.geo_areas(conn, column=column, min_count=min_count, limit=limit)
    finally:
        conn.close()
    print(f"Resolution: {resolution}")
    print()
    print("Count  H3 cell")
    print("-----  ---------------")
    for row in rows:
        print(f"{int(row['count']):5d}  {row['h3_cell']}")
    return 0


def run_geo_area(
    target: Path,
    *,
    h3_cell: str,
    limit: int | None,
    with_date: bool,
    with_coordinates: bool,
) -> int:
    resolution = h3_resolution(h3_cell)
    column = h3_column_for_resolution(resolution)
    conn = db.connect(target)
    try:
        rows = db.geo_area_files(conn, column=column, h3_cell=h3_cell, limit=limit)
    finally:
        conn.close()

    print(f"H3 cell: {h3_cell}")
    print(f"Images: {len(rows)}")
    print()
    for row in rows:
        parts = [str(row["target_path"])]
        if with_date:
            parts.append(row["taken_date"] or "-")
        if with_coordinates:
            lat = row["gps_lat"]
            lon = row["gps_lon"]
            parts.append("-" if lat is None or lon is None else f"{float(lat):.6f}, {float(lon):.6f}")
        print("\t".join(parts))
    return 0


def h3_resolution_arg(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("må være et heltall") from exc
    if not 0 <= number <= 11:
        raise argparse.ArgumentTypeError("må være mellom 0 og 11")
    return number
