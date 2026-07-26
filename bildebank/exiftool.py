from __future__ import annotations

import hashlib
import ntpath
import shutil
import stat
import subprocess
import tempfile
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path


EXIFTOOL_VERSION = "13.58"
EXIFTOOL_ARCHIVE_NAME = f"exiftool-{EXIFTOOL_VERSION}_64.zip"
EXIFTOOL_ZIP_URL = (
    "https://sourceforge.net/projects/exiftool/files/"
    f"{EXIFTOOL_ARCHIVE_NAME}/download"
)
EXIFTOOL_ARCHIVE_SHA256 = "fd3b407a01e6ffc6160f2d5fde5ff0c003f6c4c2ba85eee1ce8928ccb51fa3e6"
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


def validate_exiftool_install(
    path: Path | str,
    *,
    expected_version: str | None = None,
) -> str:
    tool_path = Path(path)
    if not tool_path.is_file():
        raise FileNotFoundError(f"Fant ikke ExifTool: {tool_path}")
    if tool_path.name.lower() == "exiftool.exe" and not (tool_path.parent / "exiftool_files").is_dir():
        raise FileNotFoundError(f"Fant ikke ExifTool-støttemappen: {tool_path.parent / 'exiftool_files'}")
    version = exiftool_version(tool_path)
    if not version:
        raise RuntimeError(f"Kunne ikke lese ExifTool-versjon: {tool_path}")
    if expected_version is not None and version != expected_version:
        raise RuntimeError(
            f"ExifTool har versjon {version!r}, forventet {expected_version!r}: {tool_path}"
        )
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
    if (destination.exists() or destination.is_symlink()) and _is_directory_link(destination):
        raise RuntimeError(f"ExifTool-mappen kan ikke være en lenke: {destination}")
    if destination.exists() and not force:
        try:
            version = validate_exiftool_install(
                tool_path,
                expected_version=EXIFTOOL_VERSION,
            )
        except (FileNotFoundError, OSError, RuntimeError):
            pass
        else:
            return ExifToolInstallResult(path=tool_path, version=version, installed=False)

    tools_root = destination.parent
    tools_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        zip_path = tmp_dir / EXIFTOOL_ARCHIVE_NAME
        _download_file(EXIFTOOL_ZIP_URL, zip_path)
        actual_hash = _sha256_file(zip_path)
        if actual_hash != EXIFTOOL_ARCHIVE_SHA256:
            raise RuntimeError(
                "ExifTool-arkivet har feil SHA-256: "
                f"forventet {EXIFTOOL_ARCHIVE_SHA256}, fikk {actual_hash}."
            )

        extract_dir = tmp_dir / "extract"
        extract_dir.mkdir()
        _safe_extract_zip(zip_path, extract_dir)

        extracted_tool = find_extracted_exiftool(extract_dir)
        extracted_files = extracted_tool.parent / "exiftool_files"
        if not extracted_files.is_dir():
            raise RuntimeError("ExifTool-zip mangler exiftool_files.")

        staging = tools_root / f".{EXIFTOOL_DIRNAME}.installing-{uuid.uuid4().hex}"
        backup = tools_root / f".{EXIFTOOL_DIRNAME}.previous-{uuid.uuid4().hex}"
        try:
            shutil.copytree(extracted_tool.parent, staging)
            staged_source = staging / extracted_tool.name
            staged_tool = staging / "exiftool.exe"
            if staged_source != staged_tool:
                if staged_tool.exists():
                    raise RuntimeError(
                        f"ExifTool-staging inneholder allerede målfilen: {staged_tool}"
                    )
                staged_source.rename(staged_tool)
            try:
                staged_tool.chmod(staged_tool.stat().st_mode | 0o755)
            except OSError:
                pass
            version = validate_exiftool_install(
                staged_tool,
                expected_version=EXIFTOOL_VERSION,
            )

            replaced = False
            try:
                if destination.exists() or destination.is_symlink():
                    if _is_directory_link(destination):
                        raise RuntimeError(
                            f"ExifTool-mappen kan ikke være en lenke: {destination}"
                        )
                    destination.rename(backup)
                    replaced = True
                staging.rename(destination)
            except BaseException:
                if replaced and backup.exists() and not destination.exists():
                    backup.rename(destination)
                raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)

        if backup.exists():
            shutil.rmtree(backup)

    return ExifToolInstallResult(path=tool_path, version=version, installed=True)


def find_extracted_exiftool(root: Path) -> Path:
    candidates = sorted(
        candidate
        for candidate in root.rglob("*")
        if candidate.is_file()
        and candidate.name.lower() in {"exiftool.exe", "exiftool(-k).exe"}
        and (candidate.parent / "exiftool_files").is_dir()
    )
    if len(candidates) != 1:
        raise RuntimeError(
            "ExifTool-zip har ikke én entydig exiftool.exe med exiftool_files."
        )
    return candidates[0]


def _download_file(url: str, destination: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Bildebank ExifTool installer"},
    )
    with (
        urllib.request.urlopen(request, timeout=60) as response,
        destination.open("wb") as output,
    ):
        shutil.copyfileobj(response, output)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract_zip(archive_path: Path, destination: Path) -> None:
    destination_resolved = destination.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            normalized_name = member.filename.replace("\\", "/")
            drive, _tail = ntpath.splitdrive(normalized_name)
            path_parts = normalized_name.split("/")
            if (
                drive
                or normalized_name.startswith("/")
                or ".." in path_parts
            ):
                raise RuntimeError(
                    f"ExifTool-arkivet har en utrygg filsti: {member.filename}"
                )
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise RuntimeError(
                    f"ExifTool-arkivet inneholder en symbolsk lenke: {member.filename}"
                )
            resolved = (destination / normalized_name).resolve()
            try:
                resolved.relative_to(destination_resolved)
            except ValueError as exc:
                raise RuntimeError(
                    f"ExifTool-arkivet har en utrygg filsti: {member.filename}"
                ) from exc
        archive.extractall(destination)


def _is_directory_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())
