from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFIG_FILENAME = "bildebank-config.toml"
ENABLED_CONFIG_SECTIONS = frozenset({"face_recognition", "image_search"})
DEFAULT_FACE_MODEL_NAME = "antelopev2"


@dataclass(frozen=True)
class FaceRecognitionConfig:
    enabled: bool = False
    provider: str = "cpu"
    model_root: Path = Path(".bildebank-insightface")
    database_dir: Path = Path(".bildebank-faces")
    model_name: str = DEFAULT_FACE_MODEL_NAME


@dataclass(frozen=True)
class OpenClipConfig:
    enabled: bool = False
    model_root: Path = Path(".bildebank-openclip")
    model_name: str = "ViT-B-32"
    pretrained: str = "laion2b_s34b_b79k"
    device: str = "auto"


@dataclass(frozen=True)
class BrowserConfig:
    hide_out_of_focus: bool = False
    manual_h3_cell: str = ""


@dataclass(frozen=True)
class AppConfig:
    face_recognition: FaceRecognitionConfig = FaceRecognitionConfig()
    openclip: OpenClipConfig = OpenClipConfig()
    browser: BrowserConfig = BrowserConfig()


def load_config(repo_root: Path) -> AppConfig:
    config_path = repo_root / CONFIG_FILENAME
    if not config_path.exists():
        return AppConfig(
            face_recognition=FaceRecognitionConfig(
                model_root=repo_root / ".bildebank-insightface",
                database_dir=Path(".bildebank-faces"),
            ),
            openclip=OpenClipConfig(model_root=repo_root / ".bildebank-openclip"),
        )
    migrate_legacy_openclip_section(config_path)
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    face_data = _section(data, "face_recognition")
    model_root = Path(str(face_data.get("model_root", ".bildebank-insightface")))
    if not model_root.is_absolute():
        model_root = repo_root / model_root
    openclip_data = _section_with_fallback(data, "image_search", "openclip")
    openclip_model_root = Path(str(openclip_data.get("model_root", ".bildebank-openclip")))
    if not openclip_model_root.is_absolute():
        openclip_model_root = repo_root / openclip_model_root
    browser_data = _section(data, "browser")
    return AppConfig(
        face_recognition=FaceRecognitionConfig(
            enabled=bool(face_data.get("enabled", False)),
            provider=str(face_data.get("provider", "cpu")),
            model_root=model_root,
            database_dir=Path(str(face_data.get("database_dir", ".bildebank-faces"))),
            model_name=str(face_data.get("model_name", DEFAULT_FACE_MODEL_NAME)),
        ),
        openclip=OpenClipConfig(
            enabled=bool(openclip_data.get("enabled", False)),
            model_root=openclip_model_root,
            model_name=str(openclip_data.get("model_name", "ViT-B-32")),
            pretrained=str(openclip_data.get("pretrained", "laion2b_s34b_b79k")),
            device=str(openclip_data.get("device", "auto")),
        ),
        browser=BrowserConfig(
            hide_out_of_focus=bool(browser_data.get("hide_out_of_focus", False)),
            manual_h3_cell=str(browser_data.get("manual_h3_cell", "")).strip(),
        ),
    )


def set_face_recognition_enabled(repo_root: Path, enabled: bool) -> Path:
    return set_config_enabled(repo_root, "face_recognition", enabled)


def set_browser_hide_out_of_focus(repo_root: Path, enabled: bool) -> Path:
    config_path = repo_root / CONFIG_FILENAME
    if not config_path.exists():
        config_path.write_text(
            "[browser]\n"
            f"hide_out_of_focus = {_toml_bool(enabled)}\n",
            encoding="utf-8",
        )
        return config_path

    migrate_legacy_openclip_section(config_path)
    text = config_path.read_text(encoding="utf-8")
    _section(tomllib.loads(text), "browser")
    config_path.write_text(
        _set_toml_bool(text, section="browser", key="hide_out_of_focus", value=enabled),
        encoding="utf-8",
    )
    return config_path


def set_browser_manual_h3_cell(repo_root: Path, h3_cell: str) -> Path:
    clean_h3_cell = h3_cell.strip()
    if clean_h3_cell:
        from .geo import h3_resolution

        h3_resolution(clean_h3_cell)
    config_path = repo_root / CONFIG_FILENAME
    if not config_path.exists():
        config_path.write_text(
            "[browser]\n"
            f'manual_h3_cell = "{_toml_string_value(clean_h3_cell)}"\n',
            encoding="utf-8",
        )
        return config_path

    migrate_legacy_openclip_section(config_path)
    text = config_path.read_text(encoding="utf-8")
    _section(tomllib.loads(text), "browser")
    config_path.write_text(
        _set_toml_string(text, section="browser", key="manual_h3_cell", value=clean_h3_cell),
        encoding="utf-8",
    )
    return config_path


def set_config_enabled(repo_root: Path, section: str, enabled: bool) -> Path:
    if section not in ENABLED_CONFIG_SECTIONS:
        allowed = ", ".join(sorted(ENABLED_CONFIG_SECTIONS))
        raise ValueError(f"Ukjent config-seksjon: {section}. Gyldige seksjoner: {allowed}.")
    config_path = repo_root / CONFIG_FILENAME
    if not config_path.exists():
        config_path.write_text(
            f"[{section}]\n"
            f"enabled = {_toml_bool(enabled)}\n",
            encoding="utf-8",
        )
        return config_path

    migrate_legacy_openclip_section(config_path)
    text = config_path.read_text(encoding="utf-8")
    _section(tomllib.loads(text), section)
    config_path.write_text(
        _set_toml_bool(text, section=section, key="enabled", value=enabled),
        encoding="utf-8",
    )
    return config_path


def migrate_legacy_openclip_section(config_path: Path) -> None:
    text = config_path.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    if "openclip" not in data or "image_search" in data:
        return
    config_path.write_text(_rename_toml_section(text, old="openclip", new="image_search"), encoding="utf-8")


def set_face_recognition_model_name(repo_root: Path, model_name: str) -> Path:
    clean_model_name = model_name.strip()
    if not clean_model_name:
        raise ValueError("InsightFace-modellnavn kan ikke være tomt.")
    config_path = repo_root / CONFIG_FILENAME
    if not config_path.exists():
        config_path.write_text(
            "[face_recognition]\n"
            f'model_name = "{_toml_string_value(clean_model_name)}"\n',
            encoding="utf-8",
        )
        return config_path

    migrate_legacy_openclip_section(config_path)
    text = config_path.read_text(encoding="utf-8")
    _section(tomllib.loads(text), "face_recognition")
    config_path.write_text(
        _set_toml_string(text, section="face_recognition", key="model_name", value=clean_model_name),
        encoding="utf-8",
    )
    return config_path


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"{CONFIG_FILENAME}: [{name}] må være en tabell.")
    return value


def _section_with_fallback(data: dict[str, Any], primary: str, fallback: str) -> dict[str, Any]:
    fallback_data = _section(data, fallback)
    if primary not in data:
        return fallback_data
    return {**fallback_data, **_section(data, primary)}


def _rename_toml_section(text: str, *, old: str, new: str) -> str:
    lines = text.splitlines(keepends=True)
    old_header = f"[{old}]"
    new_header = f"[{new}]"
    for index, line in enumerate(lines):
        stripped = line.strip()
        suffix_start = len(old_header)
        suffix_after_header = stripped[suffix_start:].lstrip()
        if stripped.startswith(old_header) and (not suffix_after_header or suffix_after_header.startswith("#")):
            prefix_length = len(line) - len(line.lstrip())
            suffix = line[line.index("]") + 1 :]
            lines[index] = f"{line[:prefix_length]}{new_header}{suffix}"
            return "".join(lines)
    return text


def _set_toml_bool(text: str, *, section: str, key: str, value: bool) -> str:
    return _set_toml_value(text, section=section, key=key, value=_toml_bool(value))


def _set_toml_string(text: str, *, section: str, key: str, value: str) -> str:
    return _set_toml_value(text, section=section, key=key, value=f'"{_toml_string_value(value)}"')


def _set_toml_value(text: str, *, section: str, key: str, value: str) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    start: int | None = None
    end = len(lines)
    section_header = f"[{section}]"
    key_prefix = f"{key} "

    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == section_header:
            start = index
            continue
        if start is not None and index > start and stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break

    new_line = f"{key} = {value}{newline}"
    if start is None:
        prefix = "" if not text or text.endswith(("\n", "\r")) else newline
        section_prefix = "" if not text else newline
        return f"{text}{prefix}{section_prefix}{section_header}{newline}{new_line}"

    for index in range(start + 1, end):
        stripped = lines[index].lstrip()
        if stripped.startswith(key_prefix) or stripped.startswith(f"{key}="):
            indent = lines[index][: len(lines[index]) - len(stripped)]
            lines[index] = f"{indent}{new_line}"
            return "".join(lines)

    lines.insert(start + 1, new_line)
    return "".join(lines)


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


def _toml_string_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
