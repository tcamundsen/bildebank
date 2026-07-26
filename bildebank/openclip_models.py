from __future__ import annotations

import hashlib
import shutil
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from .config import OpenClipConfig
from .downloaded_artifacts import (
    download_https_file,
    ensure_directory_without_links,
    reject_directory_link,
    validate_regular_file_without_links,
)


OPENCLIP_MANAGED_DIRNAME = "bildebank-models"


@dataclass(frozen=True)
class OpenClipModelSpec:
    model_name: str
    pretrained: str
    repository: str
    revision: str
    filename: str
    sha256: str
    size_bytes: int

    @property
    def download_url(self) -> str:
        return (
            f"https://huggingface.co/{self.repository}/resolve/"
            f"{self.revision}/{self.filename}"
        )

    @property
    def directory_name(self) -> str:
        return f"{self.model_name}--{self.pretrained}"


OPENCLIP_MODEL_SPECS = {
    ("ViT-B-32", "laion2b_s34b_b79k"): OpenClipModelSpec(
        model_name="ViT-B-32",
        pretrained="laion2b_s34b_b79k",
        repository="laion/CLIP-ViT-B-32-laion2B-s34B-b79K",
        revision="1a25a446712ba5ee05982a381eed697ef9b435cf",
        filename="open_clip_model.safetensors",
        sha256="ac4f8c4b88af6d963118cbf40ad93176d092abbedfcb752601ae1866352656e6",
        size_bytes=605_143_316,
    ),
    ("ViT-L-14", "laion2b_s32b_b82k"): OpenClipModelSpec(
        model_name="ViT-L-14",
        pretrained="laion2b_s32b_b82k",
        repository="laion/CLIP-ViT-L-14-laion2B-s32B-b82K",
        revision="1627032197142fbe2a7cfec626f4ced3ae60d07a",
        filename="open_clip_pytorch_model.safetensors",
        sha256="7d129ed747e0ed53e82dfcc140382b51be66b56e6a9bdc3258afd2846e3bb019",
        size_bytes=1_710_517_748,
    ),
}


@dataclass(frozen=True)
class OpenClipInstallResult:
    path: Path
    spec: OpenClipModelSpec
    installed: bool
    legacy_cache: bool = False


def openclip_model_spec(config: OpenClipConfig) -> OpenClipModelSpec | None:
    return OPENCLIP_MODEL_SPECS.get((config.model_name, config.pretrained))


def managed_openclip_model_dir(
    config: OpenClipConfig,
    spec: OpenClipModelSpec,
) -> Path:
    return config.model_root / OPENCLIP_MANAGED_DIRNAME / spec.directory_name


def managed_openclip_model_path(
    config: OpenClipConfig,
    spec: OpenClipModelSpec,
) -> Path:
    return managed_openclip_model_dir(config, spec) / spec.filename


def legacy_openclip_model_path(
    config: OpenClipConfig,
    spec: OpenClipModelSpec,
) -> Path:
    repository_dir = f"models--{spec.repository.replace('/', '--')}"
    return (
        config.model_root
        / repository_dir
        / "snapshots"
        / spec.revision
        / spec.filename
    )


def openclip_model_file_available(config: OpenClipConfig) -> bool:
    try:
        _resolve_openclip_model_file(config, check_hash=False)
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return False
    return True


def require_openclip_model_file(config: OpenClipConfig) -> Path:
    try:
        return _resolve_openclip_model_file(config, check_hash=True)
    except FileNotFoundError as exc:
        if openclip_model_spec(config) is None:
            raise ValueError(
                "Fant ikke den egendefinerte OpenCLIP-modellfilen lokalt: "
                f"{config.pretrained}"
            ) from exc
        raise ValueError(
            "Fant ikke den valgte OpenCLIP-modellen lokalt. "
            "Kjør install-openclip.ps1 fra programmappen."
        ) from exc


def install_openclip_model(config: OpenClipConfig) -> OpenClipInstallResult:
    spec = openclip_model_spec(config)
    if spec is None:
        supported = ", ".join(
            f"{item.model_name} ({item.pretrained})"
            for item in OPENCLIP_MODEL_SPECS.values()
        )
        raise ValueError(
            "Kan ikke laste ned ukjent OpenCLIP-modell sikkert: "
            f"{config.model_name} ({config.pretrained}). "
            f"Modeller med fast SHA-256: {supported}."
        )

    for candidate, legacy in (
        (managed_openclip_model_path(config, spec), False),
        (legacy_openclip_model_path(config, spec), True),
    ):
        try:
            validated = _validate_model_candidate(
                candidate,
                config=config,
                spec=spec,
                check_hash=True,
                allow_legacy_link=legacy,
            )
        except FileNotFoundError:
            continue
        except RuntimeError as exc:
            if candidate.exists() or candidate.is_symlink():
                raise ValueError(
                    f"OpenCLIP-modellfilen {candidate} avviker fra den "
                    "fastlåste modellen. Filen beholdes uendret; flytt den til "
                    "et trygt sted før du prøver igjen."
                ) from exc
            continue
        return OpenClipInstallResult(
            path=validated,
            spec=spec,
            installed=False,
            legacy_cache=legacy,
        )

    destination = managed_openclip_model_dir(config, spec)
    try:
        reject_directory_link(destination, label="OpenCLIP-modellmappen")
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    if destination.exists():
        try:
            has_content = next(destination.iterdir(), None) is not None
        except OSError as exc:
            raise ValueError(
                f"Kunne ikke kontrollere OpenCLIP-modellmappen: {destination}: {exc}"
            ) from exc
        if has_content:
            raise ValueError(
                f"OpenCLIP-modellmappen {destination} er ikke tom. "
                "Mappen beholdes uendret; flytt den til et trygt sted før du "
                "prøver igjen."
            )

    try:
        models_root = ensure_directory_without_links(
            destination.parent,
            label="OpenCLIP-modellroten",
        )
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc

    staging = models_root / (
        f".{spec.directory_name}.installing-{uuid.uuid4().hex}"
    )
    try:
        staging.mkdir()
        staged_file = staging / spec.filename
        try:
            download_https_file(
                spec.download_url,
                staged_file,
                user_agent="Bildebank OpenCLIP model installer",
                max_bytes=spec.size_bytes,
                expected_size=spec.size_bytes,
            )
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        actual_hash = _sha256_file(staged_file)
        if actual_hash != spec.sha256:
            raise ValueError(
                "OpenCLIP-modellen har feil SHA-256: "
                f"forventet {spec.sha256}, fikk {actual_hash}."
            )

        _validate_model_candidate(
            staged_file,
            config=config,
            spec=spec,
            check_hash=False,
            allow_legacy_link=False,
        )
        if destination.exists():
            destination.rmdir()
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    installed_path = managed_openclip_model_path(config, spec)
    return OpenClipInstallResult(
        path=_validate_model_candidate(
            installed_path,
            config=config,
            spec=spec,
            check_hash=False,
            allow_legacy_link=False,
        ),
        spec=spec,
        installed=True,
    )


def supported_openclip_configs(config: OpenClipConfig) -> tuple[OpenClipConfig, ...]:
    return tuple(
        replace(
            config,
            model_name=spec.model_name,
            pretrained=spec.pretrained,
        )
        for spec in OPENCLIP_MODEL_SPECS.values()
    )


def _resolve_openclip_model_file(
    config: OpenClipConfig,
    *,
    check_hash: bool,
) -> Path:
    spec = openclip_model_spec(config)
    if spec is not None:
        errors: list[Exception] = []
        for candidate, legacy in (
            (managed_openclip_model_path(config, spec), False),
            (legacy_openclip_model_path(config, spec), True),
        ):
            try:
                return _validate_model_candidate(
                    candidate,
                    config=config,
                    spec=spec,
                    check_hash=check_hash,
                    allow_legacy_link=legacy,
                )
            except (FileNotFoundError, OSError, RuntimeError) as exc:
                errors.append(exc)
        if any(not isinstance(error, FileNotFoundError) for error in errors):
            details = "; ".join(str(error) for error in errors)
            raise RuntimeError(details)
        raise FileNotFoundError(
            f"Fant ikke OpenCLIP-modellen {spec.model_name} ({spec.pretrained})."
        )

    configured = Path(config.pretrained).expanduser()
    if not configured.is_absolute():
        configured = config.model_root / configured
    configured_stat = validate_regular_file_without_links(
        configured,
        label="egendefinert OpenCLIP-modellfil",
    )
    if configured_stat.st_size == 0:
        raise RuntimeError(
            f"Den egendefinerte OpenCLIP-modellfilen er tom: {configured}"
        )
    return configured.absolute()


def _validate_model_candidate(
    candidate: Path,
    *,
    config: OpenClipConfig,
    spec: OpenClipModelSpec,
    check_hash: bool,
    allow_legacy_link: bool,
) -> Path:
    path = candidate
    if allow_legacy_link and candidate.is_symlink():
        try:
            path = candidate.resolve(strict=True)
            path.relative_to(config.model_root.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"OpenCLIP-cachelenken peker utenfor modellroten: {candidate}"
            ) from exc
    validate_regular_file_without_links(
        path,
        label="OpenCLIP-modellfilen",
        expected_size=spec.size_bytes,
    )
    if check_hash:
        actual_hash = _sha256_file(path)
        if actual_hash != spec.sha256:
            raise RuntimeError(
                "OpenCLIP-modellfilen har feil SHA-256: "
                f"forventet {spec.sha256}, fikk {actual_hash}: {path}"
            )
    return path.absolute()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
