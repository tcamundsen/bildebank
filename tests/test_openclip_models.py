from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from bildebank import openclip, openclip_models
from bildebank.config import OpenClipConfig
from bildebank.openclip_models import OpenClipModelSpec


def make_test_spec(
    content: bytes = b"model-data",
    *,
    model_name: str = "test-model",
    pretrained: str = "test-weights",
) -> OpenClipModelSpec:
    return OpenClipModelSpec(
        model_name=model_name,
        pretrained=pretrained,
        repository="owner/model",
        revision="a" * 40,
        filename="model.safetensors",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )


def config_for(tmp_path: Path, spec: OpenClipModelSpec) -> OpenClipConfig:
    return OpenClipConfig(
        model_root=tmp_path / "models",
        model_name=spec.model_name,
        pretrained=spec.pretrained,
    )


def test_official_openclip_models_are_revision_size_and_hash_pinned() -> None:
    base = openclip_models.OPENCLIP_MODEL_SPECS[
        ("ViT-B-32", "laion2b_s34b_b79k")
    ]
    large = openclip_models.OPENCLIP_MODEL_SPECS[
        ("ViT-L-14", "laion2b_s32b_b82k")
    ]

    assert base.revision == "1a25a446712ba5ee05982a381eed697ef9b435cf"
    assert base.size_bytes == 605_143_316
    assert base.sha256 == (
        "ac4f8c4b88af6d963118cbf40ad93176d092abbedfcb752601ae1866352656e6"
    )
    assert large.revision == "1627032197142fbe2a7cfec626f4ced3ae60d07a"
    assert large.size_bytes == 1_710_517_748
    assert large.sha256 == (
        "7d129ed747e0ed53e82dfcc140382b51be66b56e6a9bdc3258afd2846e3bb019"
    )
    assert f"/resolve/{base.revision}/{base.filename}" in base.download_url


def test_install_downloads_validates_and_publishes_pinned_model(
    tmp_path: Path,
) -> None:
    content = b"model-data"
    spec = make_test_spec(content)
    config = config_for(tmp_path, spec)

    def download(
        url: str,
        destination: Path,
        **kwargs: object,
    ) -> None:
        assert url == spec.download_url
        assert kwargs["max_bytes"] == len(content)
        assert kwargs["expected_size"] == len(content)
        destination.write_bytes(content)

    with (
        patch.object(
            openclip_models,
            "OPENCLIP_MODEL_SPECS",
            {(spec.model_name, spec.pretrained): spec},
        ),
        patch.object(
            openclip_models,
            "download_https_file",
            side_effect=download,
        ),
    ):
        result = openclip_models.install_openclip_model(config)

    assert result.installed
    assert result.path.read_bytes() == content
    assert openclip_models.OPENCLIP_MANAGED_DIRNAME in result.path.parts


def test_install_preserves_existing_model_with_wrong_hash(
    tmp_path: Path,
) -> None:
    spec = make_test_spec(b"expected")
    config = config_for(tmp_path, spec)
    destination = openclip_models.managed_openclip_model_path(config, spec)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"changed!")

    with (
        patch.object(
            openclip_models,
            "OPENCLIP_MODEL_SPECS",
            {(spec.model_name, spec.pretrained): spec},
        ),
        patch.object(openclip_models, "download_https_file") as download,
        pytest.raises(ValueError, match="beholdes uendret"),
    ):
        openclip_models.install_openclip_model(config)

    assert destination.read_bytes() == b"changed!"
    download.assert_not_called()


def test_existing_pinned_huggingface_cache_is_reused_without_copy(
    tmp_path: Path,
) -> None:
    content = b"model-data"
    spec = make_test_spec(content)
    config = config_for(tmp_path, spec)
    legacy = openclip_models.legacy_openclip_model_path(config, spec)
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(content)

    with (
        patch.object(
            openclip_models,
            "OPENCLIP_MODEL_SPECS",
            {(spec.model_name, spec.pretrained): spec},
        ),
        patch.object(openclip_models, "download_https_file") as download,
    ):
        result = openclip_models.install_openclip_model(config)

    assert not result.installed
    assert result.legacy_cache
    assert result.path == legacy.absolute()
    download.assert_not_called()


def test_pinned_huggingface_cache_link_is_only_followed_inside_model_root(
    tmp_path: Path,
) -> None:
    content = b"model-data"
    spec = make_test_spec(content)
    config = config_for(tmp_path, spec)
    legacy = openclip_models.legacy_openclip_model_path(config, spec)
    blob = legacy.parents[2] / "blobs" / spec.sha256
    blob.parent.mkdir(parents=True)
    blob.write_bytes(content)
    legacy.parent.mkdir(parents=True)
    try:
        legacy.symlink_to(blob)
    except OSError as exc:
        pytest.skip(f"Kan ikke opprette symlink: {exc}")

    with patch.object(
        openclip_models,
        "OPENCLIP_MODEL_SPECS",
        {(spec.model_name, spec.pretrained): spec},
    ):
        assert openclip_models.require_openclip_model_file(config) == blob.absolute()

    outside = tmp_path / "outside.safetensors"
    outside.write_bytes(content)
    legacy.unlink()
    legacy.symlink_to(outside)
    with (
        patch.object(
            openclip_models,
            "OPENCLIP_MODEL_SPECS",
            {(spec.model_name, spec.pretrained): spec},
        ),
        pytest.raises(RuntimeError, match="utenfor modellroten"),
    ):
        openclip_models.require_openclip_model_file(config)


def test_unknown_model_is_never_downloaded_automatically(
    tmp_path: Path,
) -> None:
    config = OpenClipConfig(
        model_root=tmp_path,
        model_name="unknown",
        pretrained="remote-tag",
    )
    with (
        patch.object(openclip_models, "download_https_file") as download,
        pytest.raises(ValueError, match="ukjent OpenCLIP-modell"),
    ):
        openclip_models.install_openclip_model(config)
    download.assert_not_called()


def test_explicit_local_model_file_is_allowed_for_custom_model(
    tmp_path: Path,
) -> None:
    model_file = tmp_path / "custom.safetensors"
    model_file.write_bytes(b"local")
    config = OpenClipConfig(
        model_root=tmp_path / "cache",
        model_name="custom",
        pretrained=str(model_file),
    )

    assert openclip_models.require_openclip_model_file(config) == model_file.absolute()


def test_runtime_passes_verified_local_file_and_no_cache_to_openclip(
    tmp_path: Path,
) -> None:
    model_file = tmp_path / "custom.safetensors"
    model_file.write_bytes(b"local")
    config = OpenClipConfig(
        model_root=tmp_path / "cache",
        model_name="custom",
        pretrained=str(model_file),
        device="cpu",
    )
    model = Mock()
    create_model = Mock(return_value=(model, None, "preprocess"))
    fake_openclip = SimpleNamespace(
        create_model_and_transforms=create_model,
        get_tokenizer=Mock(return_value="tokenizer"),
    )

    with patch.object(openclip, "import_open_clip", return_value=fake_openclip):
        loaded_model, preprocess = openclip.load_image_model(config)

    assert loaded_model is model
    assert preprocess == "preprocess"
    model.eval.assert_called_once_with()
    create_model.assert_called_once_with(
        "custom",
        pretrained=str(model_file.absolute()),
        device="cpu",
    )


def test_runtime_rejects_changed_pinned_model_before_openclip_load(
    tmp_path: Path,
) -> None:
    spec = make_test_spec(b"expected")
    config = config_for(tmp_path, spec)
    model_file = openclip_models.managed_openclip_model_path(config, spec)
    model_file.parent.mkdir(parents=True)
    model_file.write_bytes(b"changed!")
    create_model = Mock()
    fake_openclip = SimpleNamespace(
        create_model_and_transforms=create_model,
    )

    with (
        patch.object(
            openclip_models,
            "OPENCLIP_MODEL_SPECS",
            {(spec.model_name, spec.pretrained): spec},
        ),
        patch.object(openclip, "import_open_clip", return_value=fake_openclip),
        pytest.raises(RuntimeError, match="feil SHA-256"),
    ):
        openclip.load_image_model(config)

    create_model.assert_not_called()
