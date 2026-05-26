from __future__ import annotations

import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path


EXIFTOOL_VERSION = "13.58"
EXIFTOOL_ZIP_URL = f"https://sourceforge.net/projects/exiftool/files/exiftool-{EXIFTOOL_VERSION}_64.zip/download"
TOOLS_DIRNAME = "bildebank-tools"
EXIFTOOL_DIRNAME = "exiftool"


@dataclass(frozen=True)
class ExifToolInstallResult:
    path: Path
    version: str
    installed: bool


def managed_exiftool_dir(repo_root: Path) -> Path:
    return repo_root / TOOLS_DIRNAME / EXIFTOOL_DIRNAME


def managed_exiftool_path(repo_root: Path) -> Path:
    return managed_exiftool_dir(repo_root) / "exiftool.exe"


def validate_exiftool_install(path: Path | str) -> str:
    tool_path = Path(path)
    if not tool_path.exists():
        raise FileNotFoundError(f"Fant ikke ExifTool: {tool_path}")
    if tool_path.name.lower() == "exiftool.exe" and not (tool_path.parent / "exiftool_files").is_dir():
        raise FileNotFoundError(f"Fant ikke ExifTool-støttemappen: {tool_path.parent / 'exiftool_files'}")
    version = exiftool_version(tool_path)
    if not version:
        raise RuntimeError(f"Kunne ikke lese ExifTool-versjon: {tool_path}")
    return version


def exiftool_version(path: Path | str) -> str:
    result = subprocess.run(
        [str(path), "-ver"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"exiftool feilet med exitkode {result.returncode}")
    return result.stdout.strip()


def resolve_exiftool_path(repo_root: Path, explicit_path: Path | str | None = None) -> Path | str:
    if explicit_path is not None:
        path = Path(explicit_path).expanduser()
        validate_exiftool_install(path)
        return path

    managed = managed_exiftool_path(repo_root)
    if managed.exists():
        validate_exiftool_install(managed)
        return managed

    path_tool = shutil.which("exiftool")
    if path_tool:
        validate_exiftool_install(path_tool)
        return path_tool

    raise FileNotFoundError(
        "Fant ikke ExifTool. Kjør bildebank exiftool-install fra programmappen, "
        "eller kjør setup-windows.ps1 på nytt."
    )


def install_managed_exiftool(repo_root: Path, *, force: bool = False) -> ExifToolInstallResult:
    destination = managed_exiftool_dir(repo_root)
    tool_path = destination / "exiftool.exe"
    if tool_path.exists() and not force:
        version = validate_exiftool_install(tool_path)
        return ExifToolInstallResult(path=tool_path, version=version, installed=False)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        zip_path = tmp_dir / "exiftool.zip"
        urllib.request.urlretrieve(EXIFTOOL_ZIP_URL, zip_path)
        extract_dir = tmp_dir / "extract"
        extract_dir.mkdir()
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(extract_dir)

        extracted_tool = find_extracted_exiftool(extract_dir)
        extracted_files = extracted_tool.parent / "exiftool_files"
        if not extracted_files.is_dir():
            raise RuntimeError("ExifTool-zip mangler exiftool_files.")

        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(extracted_tool, tool_path)
        try:
            tool_path.chmod(tool_path.stat().st_mode | 0o755)
        except OSError:
            pass
        shutil.copytree(extracted_files, destination / "exiftool_files")

    version = validate_exiftool_install(tool_path)
    return ExifToolInstallResult(path=tool_path, version=version, installed=True)


def find_extracted_exiftool(root: Path) -> Path:
    candidates = sorted(root.rglob("exiftool*.exe"))
    for candidate in candidates:
        if candidate.name.lower() in {"exiftool.exe", "exiftool(-k).exe"}:
            return candidate
    if candidates:
        return candidates[0]
    raise RuntimeError("ExifTool-zip mangler exiftool.exe.")
