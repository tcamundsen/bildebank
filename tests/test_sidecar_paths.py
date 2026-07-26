from __future__ import annotations

import os
from pathlib import Path

import pytest

from bildebank.config import FaceRecognitionConfig
from bildebank.face import connect_face_db
from bildebank.openclip import connect_openclip_db
from bildebank.sidecar_paths import UnsafeSidecarPath


def test_openclip_database_symlink_is_rejected_without_touching_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "collection"
    target.mkdir()
    outside = tmp_path / "outside.sqlite3"
    original = b"must stay unchanged"
    outside.write_bytes(original)
    database_path = target / ".bilder-openclip.sqlite3"
    try:
        database_path.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink kan ikke opprettes på dette filsystemet: {exc}")

    with pytest.raises(UnsafeSidecarPath, match="symlink"):
        connect_openclip_db(target)

    assert outside.read_bytes() == original
    assert database_path.is_symlink()


def test_openclip_database_hardlink_is_rejected_without_touching_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "collection"
    target.mkdir()
    outside = tmp_path / "outside.sqlite3"
    original = b"must stay unchanged"
    outside.write_bytes(original)
    database_path = target / ".bilder-openclip.sqlite3"
    try:
        os.link(outside, database_path)
    except OSError as exc:
        pytest.skip(f"hardlink kan ikke opprettes på dette filsystemet: {exc}")

    with pytest.raises(UnsafeSidecarPath, match="hardlink"):
        connect_openclip_db(target)

    assert outside.read_bytes() == original
    assert database_path.read_bytes() == original


def test_face_database_directory_symlink_is_rejected_without_creating_database(
    tmp_path: Path,
) -> None:
    target = tmp_path / "collection"
    target.mkdir()
    outside = tmp_path / "outside-faces"
    outside.mkdir()
    database_dir = target / ".bildebank-faces"
    try:
        database_dir.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink kan ikke opprettes på dette filsystemet: {exc}")

    with pytest.raises(ValueError, match="vanlig mappe uten lenker"):
        connect_face_db(target, FaceRecognitionConfig())

    assert list(outside.iterdir()) == []
