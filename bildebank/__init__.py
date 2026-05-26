"""Bildesorteringsprogram."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import tomllib


def _version() -> str:
    try:
        return version("bildebank")
    except PackageNotFoundError:
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except OSError:
            return "0+unknown"
        return str(data["project"]["version"])


__version__ = _version()
