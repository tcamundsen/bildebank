from __future__ import annotations

from pathlib import Path

from bildebank.html_paths import display_relative_path, path_to_url, relative_to_target


def test_relative_to_target_keeps_relative_paths() -> None:
    path = Path("2024") / "01" / "image.jpg"

    assert relative_to_target(Path("/tmp/target"), path) == path


def test_relative_to_target_makes_absolute_child_relative(tmp_path: Path) -> None:
    target = tmp_path / "samling"
    image = target / "2024" / "01" / "image.jpg"

    assert relative_to_target(target, image) == Path("2024") / "01" / "image.jpg"
    assert display_relative_path(target, image) == "2024/01/image.jpg"


def test_path_to_url_quotes_parts_and_normalizes_backslashes() -> None:
    assert path_to_url(Path("2024") / "Januar bilder" / "æøå.jpg") == "2024/Januar%20bilder/%C3%A6%C3%B8%C3%A5.jpg"
    assert path_to_url(Path(r"2024\Januar bilder\image 1.jpg")) == "2024/Januar%20bilder/image%201.jpg"
