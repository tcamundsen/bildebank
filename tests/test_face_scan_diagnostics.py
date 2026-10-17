from __future__ import annotations

import hashlib
from importlib import metadata
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from tools import diagnose_face_scan as diagnostics


def test_report_hashes_configured_models_without_changing_collection(
    tmp_path: Path,
) -> None:
    config = tmp_path / "bildebank-config.toml"
    config.write_text(
        '[face_recognition]\nmodel_name = "antelopev2"\n'
        'model_root = "modeller med æ (1)"\nprovider = "cpu"\n'
        "[openclip]\nenabled = false\n",
        encoding="utf-8",
    )
    model_dir = tmp_path / "modeller med æ (1)" / "models" / "antelopev2"
    model_dir.mkdir(parents=True)
    detector = model_dir / "scrfd_10g_bnkps.onnx"
    detector.write_bytes(b"detector model")
    collection_db = tmp_path / "antelopev2.sqlite3"
    collection_db.write_bytes(b"database must remain untouched")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    def package_version(name: str) -> str:
        if name == "insightface":
            return "1.0.1"
        raise metadata.PackageNotFoundError(name)

    with patch.object(diagnostics.metadata, "version", side_effect=package_version):
        report = diagnostics.collect_report(tmp_path)

    assert "Modell: antelopev2" in report
    assert "Konfigurert provider: cpu" in report
    assert f"Modellmappe: {model_dir}" in report
    assert "insightface: 1.0.1" in report
    assert "onnxruntime-gpu: ikke installert" in report
    assert hashlib.sha256(detector.read_bytes()).hexdigest() in report
    assert {
        path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()
    } == before


def test_missing_model_files_are_reported(tmp_path: Path) -> None:
    report = diagnostics.collect_report(tmp_path)

    assert "Modell: antelopev2" in report
    assert "FEIL: Fant ingen ONNX-modellfiler" in report
    assert not (tmp_path / "bildebank-config.toml").exists()


def test_main_saves_utf8_report_in_program_directory_from_another_cwd(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    program = tmp_path / "program med æ (1)"
    program.mkdir()
    monkeypatch.setattr(diagnostics, "REPO_ROOT", program)
    monkeypatch.setattr(diagnostics, "choose_image", lambda: None)
    monkeypatch.chdir(tmp_path)

    assert diagnostics.main([]) == 0

    output = program / "face-scan-diagnose.txt"
    assert "feilsøking av face-scan" in output.read_text(encoding="utf-8")
    assert str(output) in capsys.readouterr().out
    assert not (tmp_path / output.name).exists()


def test_report_cannot_be_written_returns_error(tmp_path: Path, capsys) -> None:
    with (
        patch.object(diagnostics, "collect_report", return_value="test report\n"),
        patch.object(diagnostics, "choose_image", return_value=None),
    ):
        assert diagnostics.main(["--output", str(tmp_path)]) == 1

    assert "Kunne ikke lagre rapporten" in capsys.readouterr().err


def test_direct_image_test_preserves_image_and_uses_local_cpu_detector(
    tmp_path: Path, capsys
) -> None:
    image_path = tmp_path / "foto med æ (1).jpg"
    image_path.write_bytes(b"original image")
    detector_path = tmp_path / "scrfd_10g_bnkps.onnx"
    detector_path.write_bytes(b"model")
    image = np.zeros((24, 32, 3), dtype=np.uint8)
    detector = Mock()
    detector.session.get_providers.return_value = ["CPUExecutionProvider"]
    detector.detect.side_effect = [
        (np.array([[1, 2, 10, 12, 0.9]]), None),
        (np.array([[1, 2, 10, 12, 0.9], [12, 2, 20, 12, 0.49]]), None),
    ]
    get_model = Mock(return_value=detector)
    fake_cv2 = SimpleNamespace(
        __version__="5.0.0", __file__="cv2.pyd", getBuildInformation=lambda: "build"
    )
    with (
        patch("bildebank.face.read_image", return_value=image) as decode,
        patch.dict(
            sys.modules,
            {
                "cv2": fake_cv2,
                "insightface.model_zoo": SimpleNamespace(get_model=get_model),
            },
        ),
    ):
        diagnostics.inspect_image(image_path, detector_path)

    decode.assert_called_once_with(b"original image", image_path)
    get_model.assert_called_once_with(
        str(detector_path), providers=["CPUExecutionProvider"], download=False
    )
    detector.prepare.assert_called_once_with(
        ctx_id=-1, input_size=(640, 640), det_thresh=0.5
    )
    assert detector.detect.call_count == 2
    report = capsys.readouterr().out
    assert hashlib.sha256(b"original image").hexdigest() in report
    assert hashlib.sha256(image.tobytes()).hexdigest() in report
    assert "Deteksjonsgrense 0.5: 1 ansikter" in report
    assert "Deteksjonsgrense 0.1: 2 ansikter" in report
    assert "score=0.490000000" in report
    assert image_path.read_bytes() == b"original image"
    assert detector_path.read_bytes() == b"model"


def test_image_worker_failure_retains_partial_diagnostics(tmp_path: Path) -> None:
    completed = subprocess.CompletedProcess(
        [], 1, "Bildefil SHA256: abc\n", "decoder error"
    )
    with patch.object(diagnostics.subprocess, "run", return_value=completed):
        lines = diagnostics.image_worker_report(
            tmp_path / "foto.jpg", tmp_path / "model.onnx"
        )

    assert "Bildefil SHA256: abc" in lines
    assert any("decoder error" in line for line in lines)


def test_main_passes_selected_image_to_report(tmp_path: Path) -> None:
    image = tmp_path / "foto (1).jpg"
    output = tmp_path / "report.txt"
    with patch.object(
        diagnostics, "collect_report", return_value="test report\n"
    ) as collect:
        assert diagnostics.main([str(image), "--output", str(output)]) == 0

    collect.assert_called_once_with(diagnostics.REPO_ROOT, image)
    assert output.read_text(encoding="utf-8") == "test report\n"
