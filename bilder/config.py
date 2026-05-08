from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFIG_FILENAME = "bildebank-config.toml"


@dataclass(frozen=True)
class FaceRecognitionConfig:
    enabled: bool = False
    provider: str = "cpu"
    model_root: Path = Path(".bildebank-insightface")
    model_name: str = "buffalo_l"


@dataclass(frozen=True)
class AppConfig:
    face_recognition: FaceRecognitionConfig = FaceRecognitionConfig()


def load_config(repo_root: Path) -> AppConfig:
    config_path = repo_root / CONFIG_FILENAME
    if not config_path.exists():
        return AppConfig(
            face_recognition=FaceRecognitionConfig(
                model_root=repo_root / ".bildebank-insightface"
            )
        )
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    face_data = _section(data, "face_recognition")
    model_root = Path(str(face_data.get("model_root", ".bildebank-insightface")))
    if not model_root.is_absolute():
        model_root = repo_root / model_root
    return AppConfig(
        face_recognition=FaceRecognitionConfig(
            enabled=bool(face_data.get("enabled", False)),
            provider=str(face_data.get("provider", "cpu")),
            model_root=model_root,
            model_name=str(face_data.get("model_name", "buffalo_l")),
        )
    )


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"{CONFIG_FILENAME}: [{name}] må være en tabell.")
    return value
