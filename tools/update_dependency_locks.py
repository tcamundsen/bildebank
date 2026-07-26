#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_DIRECTORY = REPO_ROOT / "requirements"
SUPPORTED_PYTHON = (3, 13)
SUPPORTED_MACHINES = frozenset({"amd64", "x86_64"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CANONICAL_NAME_RE = re.compile(r"[-_.]+")


class LockGenerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class LockProfile:
    name: str
    filename: str
    project_requirement: str


@dataclass(frozen=True)
class LockedDistribution:
    name: str
    version: str
    sha256: str


LOCK_PROFILES = (
    LockProfile("basis", "windows-py313-base.lock", "."),
    LockProfile("InsightFace", "windows-py313-face.lock", ".[face]"),
    LockProfile("OpenCLIP", "windows-py313-openclip.lock", ".[openclip]"),
)


def main() -> int:
    try:
        require_supported_runtime()
        generated = generate_dependency_locks()
    except (LockGenerationError, OSError, json.JSONDecodeError) as exc:
        print(f"FEIL: {exc}", file=sys.stderr)
        return 2

    print()
    print("Kandidatlåser er generert:")
    for path in generated:
        print(f"  {path.relative_to(REPO_ROOT)}")
    print("Inspiser diffen og test alle tre installasjonstypene før filene committes.")
    return 0


def require_supported_runtime(
    *,
    platform_name: str | None = None,
    implementation: str | None = None,
    python_version: tuple[int, int] | None = None,
    machine: str | None = None,
) -> None:
    actual_platform = platform_name if platform_name is not None else sys.platform
    actual_implementation = implementation if implementation is not None else platform.python_implementation()
    actual_version = python_version or sys.version_info[:2]
    actual_machine = (machine if machine is not None else platform.machine()).casefold()
    if (
        actual_platform != "win32"
        or actual_implementation != "CPython"
        or actual_version != SUPPORTED_PYTHON
        or actual_machine not in SUPPORTED_MACHINES
    ):
        raise LockGenerationError(
            "Låser må genereres med 64-bit CPython 3.13 direkte i Windows. "
            f"Fant {actual_implementation} {actual_version[0]}.{actual_version[1]} "
            f"på {actual_platform}/{actual_machine or 'ukjent arkitektur'}."
        )


def generate_dependency_locks(
    *,
    repo_root: Path = REPO_ROOT,
    lock_directory: Path = LOCK_DIRECTORY,
    resolve: Callable[[LockProfile, Path, Path], dict[str, object]] | None = None,
) -> tuple[Path, ...]:
    resolver = resolve or resolve_profile
    rendered: list[tuple[Path, str]] = []
    with tempfile.TemporaryDirectory(prefix="bildebank-dependency-locks-") as temp:
        temp_directory = Path(temp)
        for profile in LOCK_PROFILES:
            print(f"== Løser {profile.name} ==")
            report = resolver(profile, repo_root, temp_directory)
            validate_report_environment(report)
            distributions = distributions_from_report(report)
            if not distributions:
                raise LockGenerationError(f"{profile.name}: pip-rapporten inneholder ingen avhengigheter.")
            content = render_lock(profile, distributions)
            rendered.append((lock_directory / profile.filename, content))

    lock_directory.mkdir(parents=True, exist_ok=True)
    temporary_paths: list[Path] = []
    try:
        for destination, content in rendered:
            temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
            temporary.write_text(content, encoding="utf-8", newline="\n")
            temporary_paths.append(temporary)
        for (destination, _content), temporary in zip(rendered, temporary_paths, strict=True):
            os.replace(temporary, destination)
    finally:
        for temporary in temporary_paths:
            temporary.unlink(missing_ok=True)
    return tuple(destination for destination, _content in rendered)


def resolve_profile(profile: LockProfile, repo_root: Path, temp_directory: Path) -> dict[str, object]:
    report_path = temp_directory / f"{profile.filename}.json"
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--dry-run",
        "--ignore-installed",
        "--only-binary=:all:",
        "--report",
        str(report_path),
        profile.project_requirement,
        "setuptools>=77",
    ]
    result = subprocess.run(command, cwd=repo_root, check=False)
    if result.returncode != 0:
        raise LockGenerationError(f"{profile.name}: pip avsluttet med kode {result.returncode}.")
    if not report_path.is_file():
        raise LockGenerationError(f"{profile.name}: pip opprettet ikke forventet rapport.")
    loaded = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise LockGenerationError(f"{profile.name}: pip-rapporten er ikke et JSON-objekt.")
    return loaded


def validate_report_environment(report: dict[str, object]) -> None:
    if report.get("version") != "1":
        raise LockGenerationError(f"Ukjent pip-rapportversjon: {report.get('version')!r}.")
    environment = report.get("environment")
    if not isinstance(environment, dict):
        raise LockGenerationError("Pip-rapporten mangler miljøinformasjon.")
    python_version = environment.get("python_version")
    machine = environment.get("platform_machine")
    require_supported_runtime(
        platform_name=string_value(environment, "sys_platform"),
        implementation=string_value(environment, "platform_python_implementation"),
        python_version=parse_python_version(python_version),
        machine=machine if isinstance(machine, str) else "",
    )


def parse_python_version(value: object) -> tuple[int, int]:
    if not isinstance(value, str):
        return (0, 0)
    parts = value.split(".")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return (0, 0)
    return (int(parts[0]), int(parts[1]))


def distributions_from_report(report: dict[str, object]) -> tuple[LockedDistribution, ...]:
    raw_items = report.get("install")
    if not isinstance(raw_items, list):
        raise LockGenerationError("Pip-rapporten mangler installasjonslisten.")
    distributions: dict[str, LockedDistribution] = {}
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise LockGenerationError("Pip-rapporten har en ugyldig installasjonspost.")
        metadata = raw_item.get("metadata")
        download_info = raw_item.get("download_info")
        if not isinstance(metadata, dict) or not isinstance(download_info, dict):
            raise LockGenerationError("Pip-rapporten har en post uten metadata eller nedlastingsdata.")
        name = canonicalize_name(string_value(metadata, "name"))
        if name == "bildebank":
            validate_local_project(download_info)
            continue
        version = string_value(metadata, "version")
        if raw_item.get("is_yanked") is True:
            raise LockGenerationError(f"{name}=={version} er trukket tilbake (yanked).")
        sha256 = wheel_sha256(name, version, download_info)
        distribution = LockedDistribution(name=name, version=version, sha256=sha256)
        if name in distributions:
            raise LockGenerationError(f"Pip-rapporten inneholder flere poster for {name}.")
        distributions[name] = distribution
    return tuple(distributions[name] for name in sorted(distributions))


def validate_local_project(download_info: dict[str, object]) -> None:
    directory_info = download_info.get("dir_info")
    url = download_info.get("url")
    if not isinstance(directory_info, dict) or not isinstance(url, str) or not url.startswith("file:"):
        raise LockGenerationError("Bildebank-posten i pip-rapporten er ikke det lokale prosjektet.")


def wheel_sha256(name: str, version: str, download_info: dict[str, object]) -> str:
    url = download_info.get("url")
    if not isinstance(url, str) or urlsplit(url).scheme != "https":
        raise LockGenerationError(f"{name}=={version} kommer ikke fra en HTTPS-adresse.")
    if not urlsplit(url).path.casefold().endswith(".whl"):
        raise LockGenerationError(f"{name}=={version} ble ikke løst til et binærhjul.")
    archive_info = download_info.get("archive_info")
    if not isinstance(archive_info, dict):
        raise LockGenerationError(f"{name}=={version} mangler arkivinformasjon.")
    hashes = archive_info.get("hashes")
    if not isinstance(hashes, dict):
        raise LockGenerationError(f"{name}=={version} mangler SHA-256.")
    sha256 = hashes.get("sha256")
    if not isinstance(sha256, str) or SHA256_RE.fullmatch(sha256.casefold()) is None:
        raise LockGenerationError(f"{name}=={version} har ugyldig SHA-256.")
    return sha256.casefold()


def render_lock(profile: LockProfile, distributions: tuple[LockedDistribution, ...]) -> str:
    lines = [
        f"# Kandidatlås for Bildebank {profile.name}.",
        "# Generert av tools/update_dependency_locks.py.",
        "# Målplattform: Windows x64, CPython 3.13.",
        "# Ikke rediger manuelt. Test før filen committes.",
        "--only-binary=:all:",
        "--require-hashes",
        "",
    ]
    for distribution in distributions:
        lines.extend(
            (
                f"{distribution.name}=={distribution.version} \\",
                f"    --hash=sha256:{distribution.sha256}",
            )
        )
    return "\n".join(lines) + "\n"


def canonicalize_name(value: str) -> str:
    name = CANONICAL_NAME_RE.sub("-", value).casefold()
    if not name:
        raise LockGenerationError("Pip-rapporten inneholder et tomt pakkenavn.")
    return name


def string_value(mapping: dict[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise LockGenerationError(f"Pip-rapporten mangler tekstfeltet {key!r}.")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
