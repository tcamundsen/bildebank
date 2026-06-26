from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from bildebank import db
from bildebank.launcher import (
    LauncherConfig,
    create_command,
    default_collection_path,
    geo_scan_command,
    import_command,
    is_collection_created,
    load_launcher_config,
    make_thumbnails_command,
    save_launcher_config,
    source_is_collection_or_inside,
    suggest_import_name,
)


def test_default_collection_path_uses_home_kode_bilde_samling() -> None:
    with patch("pathlib.Path.home", return_value=Path(r"C:\Users\tom")):
        assert default_collection_path() == Path(r"C:\Users\tom") / "kode" / "bilde-samling"


def test_load_config_uses_default_when_file_is_missing(tmp_path: Path) -> None:
    with patch("pathlib.Path.home", return_value=tmp_path / "home"):
        config = load_launcher_config(tmp_path / "missing.json")

    assert config.collection_path == tmp_path / "home" / "kode" / "bilde-samling"


def test_save_and_load_launcher_config(tmp_path: Path) -> None:
    config_path = tmp_path / "Bildebank" / "launcher.json"
    collection_path = tmp_path / "samling"

    save_launcher_config(LauncherConfig(collection_path=collection_path), config_path)

    assert load_launcher_config(config_path).collection_path == collection_path


def test_suggest_import_name_uses_last_folder_name() -> None:
    assert suggest_import_name(Path(r"D:\Bilder\Sommer 2024")) == "Sommer 2024"


def test_source_is_collection_or_inside_rejects_collection_and_child(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    source = tmp_path / "bilder"
    child = collection / "2024"
    collection.mkdir()
    source.mkdir()
    child.mkdir()

    assert source_is_collection_or_inside(collection, collection)
    assert source_is_collection_or_inside(child, collection)
    assert not source_is_collection_or_inside(source, collection)


def test_is_collection_created_uses_bildebank_database_marker(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    collection.mkdir()
    assert not is_collection_created(collection)

    db.init_database(collection)

    assert is_collection_created(collection)


def test_launcher_commands_use_existing_cli_semantics(tmp_path: Path) -> None:
    collection = tmp_path / "samling"
    source = tmp_path / "bilder"

    assert create_command(collection)[-2:] == ["create", str(collection)]
    import_args = import_command(collection, source, "Sommer 2024")
    assert import_args[-6:] == [
        "--target",
        str(collection),
        "import",
        "--name",
        "Sommer 2024",
        str(source),
    ]
    assert os.path.basename(import_args[0]).startswith("python")

    assert geo_scan_command(collection)[-3:] == ["--target", str(collection), "geo-scan"]
    assert make_thumbnails_command(collection)[-3:] == [
        "--target",
        str(collection),
        "make-thumbnails",
    ]
