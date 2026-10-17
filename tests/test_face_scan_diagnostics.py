from __future__ import annotations

import hashlib
from importlib import metadata
from pathlib import Path
from unittest.mock import patch

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
    monkeypatch.chdir(tmp_path)

    assert diagnostics.main([]) == 0

    output = program / "face-scan-diagnose.txt"
    assert "feilsøking av face-scan" in output.read_text(encoding="utf-8")
    assert str(output) in capsys.readouterr().out
    assert not (tmp_path / output.name).exists()


def test_report_cannot_be_written_returns_error(tmp_path: Path, capsys) -> None:
    with patch.object(diagnostics, "collect_report", return_value="test report\n"):
        assert diagnostics.main(["--output", str(tmp_path)]) == 1

    assert "Kunne ikke lagre rapporten" in capsys.readouterr().err
