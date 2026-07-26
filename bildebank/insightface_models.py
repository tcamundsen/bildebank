from __future__ import annotations

import hashlib
import shutil
import stat
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .config import FaceRecognitionConfig
from .downloaded_artifacts import (
    download_https_file,
    ensure_directory_without_links,
    reject_directory_link,
)


INSIGHTFACE_MODEL_RELEASE_URL = (
    "https://github.com/deepinsight/insightface/releases/download/v0.7"
)


@dataclass(frozen=True)
class InsightFaceModelFile:
    name: str
    size_bytes: int


@dataclass(frozen=True)
class InsightFaceModelSpec:
    name: str
    archive_sha256: str
    archive_size_bytes: int
    archive_prefix: str
    files: tuple[InsightFaceModelFile, ...]

    @property
    def archive_url(self) -> str:
        return f"{INSIGHTFACE_MODEL_RELEASE_URL}/{self.name}.zip"


INSIGHTFACE_MODEL_SPECS = {
    "antelopev2": InsightFaceModelSpec(
        name="antelopev2",
        archive_sha256=(
            "8e182f14fc6e80b3bfa375b33eb6cff7ee05d8ef7633e738d1c89021dcf0c5c5"
        ),
        archive_size_bytes=360_662_982,
        archive_prefix="antelopev2/",
        files=(
            InsightFaceModelFile("1k3d68.onnx", 143_607_619),
            InsightFaceModelFile("2d106det.onnx", 5_030_888),
            InsightFaceModelFile("genderage.onnx", 1_322_532),
            InsightFaceModelFile("glintr100.onnx", 260_665_334),
            InsightFaceModelFile("scrfd_10g_bnkps.onnx", 16_923_827),
        ),
    ),
    "buffalo_l": InsightFaceModelSpec(
        name="buffalo_l",
        archive_sha256=(
            "80ffe37d8a5940d59a7384c201a2a38d4741f2f3c51eef46ebb28218a7b0ca2f"
        ),
        archive_size_bytes=288_621_354,
        archive_prefix="",
        files=(
            InsightFaceModelFile("1k3d68.onnx", 143_607_619),
            InsightFaceModelFile("2d106det.onnx", 5_030_888),
            InsightFaceModelFile("det_10g.onnx", 16_923_827),
            InsightFaceModelFile("genderage.onnx", 1_322_532),
            InsightFaceModelFile("w600k_r50.onnx", 174_383_860),
        ),
    ),
}


def insightface_model_dir(config: FaceRecognitionConfig) -> Path:
    return config.model_root / "models" / config.model_name


def insightface_model_files_exist(config: FaceRecognitionConfig) -> bool:
    model_dir = insightface_model_dir(config)
    spec = INSIGHTFACE_MODEL_SPECS.get(config.model_name)
    if spec is None:
        try:
            return model_dir.is_dir() and any(model_dir.rglob("*.onnx"))
        except OSError:
            return False
    return _model_files_match_spec(model_dir, spec)


def ensure_insightface_model(config: FaceRecognitionConfig) -> Path:
    model_dir = insightface_model_dir(config)
    if insightface_model_files_exist(config):
        return model_dir
    return install_insightface_model(config)


def install_insightface_model(config: FaceRecognitionConfig) -> Path:
    spec = INSIGHTFACE_MODEL_SPECS.get(config.model_name)
    if spec is None:
        supported = ", ".join(sorted(INSIGHTFACE_MODEL_SPECS))
        raise ValueError(
            f"Kan ikke laste ned ukjent InsightFace-modell {config.model_name!r} sikkert. "
            f"Modeller med fast SHA-256: {supported}."
        )

    destination = insightface_model_dir(config)
    try:
        reject_directory_link(destination, label="InsightFace-modellmappen")
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    if destination.exists():
        if not destination.is_dir():
            raise ValueError(
                f"InsightFace-modellstien finnes, men er ikke en mappe: {destination}"
            )
        try:
            destination_has_content = next(destination.iterdir(), None) is not None
        except OSError as exc:
            raise ValueError(
                f"Kunne ikke kontrollere InsightFace-modellmappen: {destination}: {exc}"
            ) from exc
        if destination_has_content:
            raise ValueError(
                f"InsightFace-modellmappen {destination} er ufullstendig eller "
                "avviker fra den fastlåste modellen. Mappen beholdes uendret; "
                "flytt den til et trygt sted før du prøver nedlasting på nytt."
            )

    try:
        models_root = ensure_directory_without_links(
            destination.parent,
            label="InsightFace-modellroten",
        )
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    with tempfile.TemporaryDirectory() as tmp:
        archive_path = Path(tmp) / f"{spec.name}.zip"
        try:
            _download_file(
                spec.archive_url,
                archive_path,
                expected_size=spec.archive_size_bytes,
            )
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
        archive_size = archive_path.stat().st_size
        if archive_size != spec.archive_size_bytes:
            raise ValueError(
                f"InsightFace-arkivet har feil størrelse: forventet "
                f"{spec.archive_size_bytes}, fikk {archive_size}."
            )
        actual_hash = _sha256_file(archive_path)
        if actual_hash != spec.archive_sha256:
            raise ValueError(
                "InsightFace-arkivet har feil SHA-256: "
                f"forventet {spec.archive_sha256}, fikk {actual_hash}."
            )

        staging = models_root / f".{spec.name}.installing-{uuid.uuid4().hex}"
        backup = models_root / f".{spec.name}.previous-{uuid.uuid4().hex}"
        try:
            staging.mkdir()
            _extract_model_archive(archive_path, staging, spec)
            if not _model_files_match_spec(staging, spec):
                raise ValueError(
                    f"InsightFace-arkivet ga ikke en komplett {spec.name!r}-modell."
                )

            replaced = False
            try:
                if destination.exists() or destination.is_symlink():
                    try:
                        reject_directory_link(
                            destination,
                            label="InsightFace-modellmappen",
                        )
                    except RuntimeError as exc:
                        raise ValueError(str(exc)) from exc
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
    return destination


def _model_files_match_spec(model_dir: Path, spec: InsightFaceModelSpec) -> bool:
    try:
        if not model_dir.is_dir() or _is_directory_link(model_dir):
            return False
        expected_names = {model_file.name for model_file in spec.files}
        actual_names = {
            path.name
            for path in model_dir.glob("*.onnx")
            if path.is_file() and not path.is_symlink()
        }
        if actual_names != expected_names:
            return False
        for model_file in spec.files:
            path = model_dir / model_file.name
            if (
                not path.is_file()
                or path.is_symlink()
                or path.stat().st_size != model_file.size_bytes
            ):
                return False
    except OSError:
        return False
    return True


def _download_file(url: str, destination: Path, *, expected_size: int) -> None:
    download_https_file(
        url,
        destination,
        user_agent="Bildebank InsightFace model installer",
        max_bytes=expected_size,
        expected_size=expected_size,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_model_archive(
    archive_path: Path,
    destination: Path,
    spec: InsightFaceModelSpec,
) -> None:
    expected = {
        f"{spec.archive_prefix}{model_file.name}": model_file
        for model_file in spec.files
    }
    allowed_directory = spec.archive_prefix.rstrip("/")
    with zipfile.ZipFile(archive_path) as archive:
        members: dict[str, zipfile.ZipInfo] = {}
        for member in archive.infolist():
            normalized_name = member.filename.replace("\\", "/")
            if member.is_dir() and normalized_name.rstrip("/") == allowed_directory:
                continue
            if normalized_name not in expected or normalized_name in members:
                raise ValueError(
                    f"InsightFace-arkivet har en uventet fil: {member.filename}"
                )
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(
                    f"InsightFace-arkivet inneholder en symbolsk lenke: {member.filename}"
                )
            model_file = expected[normalized_name]
            if member.file_size != model_file.size_bytes:
                raise ValueError(
                    f"InsightFace-arkivfilen {member.filename!r} har feil størrelse."
                )
            members[normalized_name] = member

        missing = sorted(set(expected) - set(members))
        if missing:
            raise ValueError(
                "InsightFace-arkivet mangler forventede filer: " + ", ".join(missing)
            )

        for archive_name, model_file in expected.items():
            output_path = destination / model_file.name
            with archive.open(members[archive_name]) as source, output_path.open("xb") as output:
                shutil.copyfileobj(source, output)


def _is_directory_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())
