from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from bildebank.collection_paths import (
    COLLECTION_FILE_MISSING,
    COLLECTION_FILE_NOT_REGULAR,
    COLLECTION_FILE_OK,
    InvalidCollectionRelativePath,
    inspect_collection_file,
    inspect_existing_collection_path_components,
    is_active_collection_file_path,
    is_deleted_collection_file_path,
    is_reparse_stat,
    parse_collection_relative_path,
)


@pytest.mark.parametrize(
    "value",
    (
        "/outside.jpg",
        "C:/outside.jpg",
        "C:outside.jpg",
        r"2024\01\outside.jpg",
        "2024/../outside.jpg",
        "2024/./outside.jpg",
        "2024//outside.jpg",
        "",
    ),
)
def test_parse_collection_relative_path_rejects_unsafe_forms(
    value: str,
) -> None:
    with pytest.raises(InvalidCollectionRelativePath):
        parse_collection_relative_path(value)


def test_collection_file_layouts_are_explicit() -> None:
    assert is_active_collection_file_path(Path("2024/01/image.jpg"))
    assert is_active_collection_file_path(Path("udatert/image.jpg"))
    assert not is_active_collection_file_path(Path("2024/13/image.jpg"))
    assert not is_active_collection_file_path(Path("2024/01/nested/image.jpg"))

    assert is_deleted_collection_file_path(
        Path("deleted/2024/01/image.jpg")
    )
    assert is_deleted_collection_file_path(Path("deleted/udatert/image.jpg"))
    assert not is_deleted_collection_file_path(Path("deleted/image.jpg"))


def test_existing_collection_path_components_do_not_follow_symlink(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    target.mkdir()
    (outside / "01").mkdir(parents=True)
    try:
        (target / "2024").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Kan ikke opprette test-symlink: {exc}")

    issue = inspect_existing_collection_path_components(
        target,
        Path("2024/01/image.jpg"),
    )

    assert issue is not None
    assert issue.path == target / "2024"
    assert "symlink" in issue.reason


def test_existing_component_check_rejects_path_outside_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(InvalidCollectionRelativePath):
        inspect_existing_collection_path_components(
            target,
            Path("../outside.jpg"),
        )


def test_collection_file_inspection_reports_regular_missing_and_directory(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    regular = target / "2024" / "01" / "regular.jpg"
    directory = target / "2024" / "01" / "directory.jpg"
    regular.parent.mkdir(parents=True)
    regular.write_bytes(b"regular")
    directory.mkdir()

    regular_result = inspect_collection_file(
        target,
        Path("2024/01/regular.jpg"),
    )
    missing_result = inspect_collection_file(
        target,
        Path("2024/01/missing.jpg"),
    )
    directory_result = inspect_collection_file(
        target,
        Path("2024/01/directory.jpg"),
    )

    assert regular_result.status == COLLECTION_FILE_OK
    assert regular_result.size_bytes == len(b"regular")
    assert missing_result.status == COLLECTION_FILE_MISSING
    assert directory_result.status == COLLECTION_FILE_NOT_REGULAR


def test_collection_file_inspection_does_not_follow_final_symlink(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    outside = tmp_path / "outside.jpg"
    linked = target / "2024" / "01" / "linked.jpg"
    linked.parent.mkdir(parents=True)
    outside.write_bytes(b"outside")
    try:
        linked.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"Kan ikke opprette test-symlink: {exc}")

    result = inspect_collection_file(
        target,
        Path("2024/01/linked.jpg"),
    )

    assert result.status == COLLECTION_FILE_NOT_REGULAR
    assert result.size_bytes is None


def test_windows_reparse_attribute_is_detected_portably() -> None:
    assert is_reparse_stat(SimpleNamespace(st_file_attributes=0x400))
    assert not is_reparse_stat(SimpleNamespace(st_file_attributes=0))
