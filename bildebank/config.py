from __future__ import annotations

import tomllib
import datetime as dt
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .value_parsing import require_float


CONFIG_FILENAME = "bildebank-config.toml"
ENABLED_CONFIG_SECTIONS = frozenset({"face_recognition", "image_search"})
DEFAULT_FACE_MODEL_NAME = "antelopev2"
HOTKEY_KEYS = ("1", "2", "3", "4", "5")
HOTKEY_ACTIONS = frozenset({"", "h3", "manual_date", "person", "tag"})


@dataclass(frozen=True)
class FaceRecognitionConfig:
    enabled: bool = False
    provider: str = "cpu"
    model_root: Path = Path(".bildebank-insightface")
    database_dir: Path = Path(".bildebank-faces")
    model_name: str = DEFAULT_FACE_MODEL_NAME
    suggest_threshold: float = 0.6


@dataclass(frozen=True)
class OpenClipConfig:
    enabled: bool = False
    model_root: Path = Path(".bildebank-openclip")
    model_name: str = "ViT-B-32"
    pretrained: str = "laion2b_s34b_b79k"
    device: str = "auto"


@dataclass(frozen=True)
class BrowserHotkeyConfig:
    action: str = ""
    h3_cell: str = ""
    person_name: str = ""
    tag_name: str = ""
    mode: str = ""
    date: str = ""
    uncertainty: str = ""
    date_from: str = ""
    date_to: str = ""
    note: str = ""


def default_browser_hotkeys() -> dict[str, BrowserHotkeyConfig]:
    return {key: BrowserHotkeyConfig() for key in HOTKEY_KEYS}


@dataclass(frozen=True)
class BrowserConfig:
    hide_out_of_focus: bool = False
    manual_person_controls_enabled: bool = True
    hotkey_hints_enabled: bool = False
    hotkeys: dict[str, BrowserHotkeyConfig] = field(default_factory=default_browser_hotkeys)


@dataclass(frozen=True)
class AppConfig:
    face_recognition: FaceRecognitionConfig = FaceRecognitionConfig()
    openclip: OpenClipConfig = OpenClipConfig()
    browser: BrowserConfig = field(default_factory=BrowserConfig)


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
    suggest_threshold = validate_face_suggest_threshold(face_data.get("suggest_threshold", 0.6))
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
            suggest_threshold=suggest_threshold,
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
            manual_person_controls_enabled=bool(browser_data.get("manual_person_controls_enabled", True)),
            hotkey_hints_enabled=bool(browser_data.get("hotkey_hints_enabled", False)),
            hotkeys=parse_browser_hotkeys(browser_data.get("hotkeys", {})),
        ),
    )


def parse_browser_hotkeys(value: object) -> dict[str, BrowserHotkeyConfig]:
    if value is None:
        return default_browser_hotkeys()
    if not isinstance(value, dict):
        raise ValueError(f"{CONFIG_FILENAME}: [browser.hotkeys] må være en tabell.")
    hotkeys = default_browser_hotkeys()
    for key, raw_config in value.items():
        clean_key = str(key).strip()
        if clean_key not in HOTKEY_KEYS:
            raise ValueError(f"Ukjent hurtigtast: {clean_key}. Gyldige taster er 1, 2, 3, 4 og 5.")
        if not isinstance(raw_config, dict):
            raise ValueError(f"Hurtigtast {clean_key} må være en tabell.")
        hotkeys[clean_key] = browser_hotkey_from_mapping(raw_config)
    return hotkeys


def browser_hotkey_from_mapping(data: dict[str, object]) -> BrowserHotkeyConfig:
    action = str(data.get("action", "")).strip()
    if action == "off":
        action = ""
    if action not in HOTKEY_ACTIONS:
        raise ValueError(f"Ukjent hurtigtasthandling: {action}")
    config = BrowserHotkeyConfig(
        action=action,
        h3_cell=str(data.get("h3_cell", "")).strip(),
        person_name=str(data.get("person_name", "")).strip(),
        tag_name=str(data.get("tag_name", "")).strip(),
        mode=str(data.get("mode", "")).strip(),
        date=str(data.get("date", "")).strip(),
        uncertainty=str(data.get("uncertainty", "")).strip(),
        date_from=str(data.get("date_from", "")).strip(),
        date_to=str(data.get("date_to", "")).strip(),
        note=str(data.get("note", "")).strip(),
    )
    validate_browser_hotkey(config)
    return config


def validate_browser_hotkey(config: BrowserHotkeyConfig) -> None:
    if config.action == "":
        return
    if config.action == "h3":
        if not config.h3_cell:
            raise ValueError("H3-hurtigtast mangler H3-celle.")
        from .geo import h3_resolution

        h3_resolution(config.h3_cell)
        return
    if config.action == "person":
        if not config.person_name:
            raise ValueError("Person-hurtigtast mangler personnavn.")
        return
    if config.action == "tag":
        from .db import normalize_tag_name

        try:
            normalize_tag_name(config.tag_name)
        except ValueError as exc:
            raise ValueError(f"Tagg-hurtigtast har ugyldig taggnavn: {exc}") from exc
        return
    if config.action == "manual_date":
        validate_manual_date_hotkey(config)
        return
    raise ValueError(f"Ukjent hurtigtasthandling: {config.action}")


def validate_manual_date_hotkey(config: BrowserHotkeyConfig) -> None:
    if config.mode == "exact":
        parse_iso_date(config.date, "Dato")
        return
    if config.mode == "uncertain":
        parse_iso_date(config.date, "Dato")
        if not config.uncertainty:
            raise ValueError("Usikkerhet mangler.")
        if config.uncertainty not in {"1d", "1w", "1m", "1y"}:
            raise ValueError("Ugyldig usikkerhet.")
        return
    if config.mode == "between":
        start = parse_iso_date(config.date_from, "Fra-dato")
        end = parse_iso_date(config.date_to, "Til-dato")
        if start > end:
            raise ValueError("Fra-dato kan ikke være etter til-dato.")
        return
    raise ValueError("Ugyldig datomodus.")


def parse_iso_date(value: str, label: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{label} må være på formen YYYY-MM-DD.") from exc


def set_face_recognition_enabled(repo_root: Path, enabled: bool) -> Path:
    return set_config_enabled(repo_root, "face_recognition", enabled)


def set_image_search_enabled(repo_root: Path, enabled: bool) -> Path:
    return set_config_enabled(repo_root, "image_search", enabled)


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


def set_browser_manual_person_controls_enabled(repo_root: Path, enabled: bool) -> Path:
    config_path = repo_root / CONFIG_FILENAME
    if not config_path.exists():
        config_path.write_text(
            "[browser]\n"
            f"manual_person_controls_enabled = {_toml_bool(enabled)}\n",
            encoding="utf-8",
        )
        return config_path

    migrate_legacy_openclip_section(config_path)
    text = config_path.read_text(encoding="utf-8")
    _section(tomllib.loads(text), "browser")
    config_path.write_text(
        _set_toml_bool(text, section="browser", key="manual_person_controls_enabled", value=enabled),
        encoding="utf-8",
    )
    return config_path


def set_browser_hotkey_hints_enabled(repo_root: Path, enabled: bool) -> Path:
    config_path = repo_root / CONFIG_FILENAME
    if not config_path.exists():
        config_path.write_text(
            "[browser]\n"
            f"hotkey_hints_enabled = {_toml_bool(enabled)}\n",
            encoding="utf-8",
        )
        return config_path

    migrate_legacy_openclip_section(config_path)
    text = config_path.read_text(encoding="utf-8")
    _section(tomllib.loads(text), "browser")
    config_path.write_text(
        _set_toml_bool(text, section="browser", key="hotkey_hints_enabled", value=enabled),
        encoding="utf-8",
    )
    return config_path


def set_browser_hotkey(repo_root: Path, key: str, hotkey: BrowserHotkeyConfig) -> Path:
    clean_key = key.strip()
    if clean_key not in HOTKEY_KEYS:
        raise ValueError(f"Ukjent hurtigtast: {clean_key}. Gyldige taster er 1, 2, 3, 4 og 5.")
    validate_browser_hotkey(hotkey)
    config_path = repo_root / CONFIG_FILENAME
    value = _toml_inline_table(browser_hotkey_to_toml_values(hotkey))
    if not config_path.exists():
        config_path.write_text(
            "[browser.hotkeys]\n"
            f'"{clean_key}" = {value}\n',
            encoding="utf-8",
        )
        return config_path

    migrate_legacy_openclip_section(config_path)
    text = config_path.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    _section(data, "browser")
    parse_browser_hotkeys(_section(_section(data, "browser"), "hotkeys") if "hotkeys" in _section(data, "browser") else {})
    config_path.write_text(
        _set_toml_value(text, section="browser.hotkeys", key=f'"{clean_key}"', value=value),
        encoding="utf-8",
    )
    return config_path


def browser_hotkey_to_toml_values(hotkey: BrowserHotkeyConfig) -> dict[str, object]:
    if not hotkey.action:
        return {"action": ""}
    if hotkey.action == "h3":
        return {"action": "h3", "h3_cell": hotkey.h3_cell}
    if hotkey.action == "person":
        return {"action": "person", "person_name": hotkey.person_name}
    if hotkey.action == "tag":
        from .db import normalize_tag_name

        return {"action": "tag", "tag_name": normalize_tag_name(hotkey.tag_name)}
    if hotkey.action == "manual_date":
        values: dict[str, object] = {"action": "manual_date", "mode": hotkey.mode}
        if hotkey.mode in {"exact", "uncertain"}:
            values["date"] = hotkey.date
        if hotkey.mode == "uncertain":
            values["uncertainty"] = hotkey.uncertainty
        if hotkey.mode == "between":
            values["date_from"] = hotkey.date_from
            values["date_to"] = hotkey.date_to
        if hotkey.note:
            values["note"] = hotkey.note
        return values
    raise ValueError(f"Ukjent hurtigtasthandling: {hotkey.action}")


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


def validate_face_suggest_threshold(value: object) -> float:
    try:
        threshold = require_float(value, "threshold")
    except ValueError as exc:
        raise ValueError("Threshold må være et tall mellom 0.0 og 1.0.") from exc
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise ValueError("Threshold må være et endelig tall mellom 0.0 og 1.0.")
    return threshold


def set_face_suggest_threshold(repo_root: Path, threshold: float) -> Path:
    clean_threshold = validate_face_suggest_threshold(threshold)
    config_path = repo_root / CONFIG_FILENAME
    if not config_path.exists():
        config_path.write_text(
            "[face_recognition]\n"
            f"suggest_threshold = {clean_threshold}\n",
            encoding="utf-8",
        )
        return config_path
    migrate_legacy_openclip_section(config_path)
    text = config_path.read_text(encoding="utf-8")
    _section(tomllib.loads(text), "face_recognition")
    config_path.write_text(
        _set_toml_value(text, section="face_recognition", key="suggest_threshold", value=str(clean_threshold)),
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


def _toml_inline_table(values: dict[str, object]) -> str:
    parts = []
    for key, value in values.items():
        if isinstance(value, bool):
            rendered = _toml_bool(value)
        else:
            rendered = f'"{_toml_string_value(str(value))}"'
        parts.append(f"{key} = {rendered}")
    return "{ " + ", ".join(parts) + " }"
