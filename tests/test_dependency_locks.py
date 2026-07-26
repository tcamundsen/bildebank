from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def load_lock_module():
    path = Path(__file__).resolve().parents[1] / "tools" / "update_dependency_locks.py"
    spec = importlib.util.spec_from_file_location("update_dependency_locks", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sample_report(*, url: str | None = None, sha256: str = "a" * 64) -> dict[str, object]:
    return {
        "version": "1",
        "environment": {
            "platform_machine": "AMD64",
            "platform_python_implementation": "CPython",
            "python_version": "3.13",
            "sys_platform": "win32",
        },
        "install": [
            {
                "download_info": {
                    "url": "file:///C:/Users/Tom/kode/bildebank",
                    "dir_info": {"editable": False},
                },
                "metadata": {"name": "bildebank", "version": "0.9.0"},
            },
            {
                "download_info": {
                    "url": url or "https://files.pythonhosted.org/packages/Pillow-12.2.0-cp313-win_amd64.whl",
                    "archive_info": {"hashes": {"sha256": sha256}},
                },
                "metadata": {"name": "Pillow", "version": "12.2.0"},
            },
        ],
    }


def test_runtime_guard_requires_windows_cpython_313_x64() -> None:
    locks = load_lock_module()

    locks.require_supported_runtime(
        platform_name="win32",
        implementation="CPython",
        python_version=(3, 13),
        machine="AMD64",
    )
    with pytest.raises(locks.LockGenerationError, match="CPython 3.13 direkte i Windows"):
        locks.require_supported_runtime(
            platform_name="linux",
            implementation="CPython",
            python_version=(3, 13),
            machine="x86_64",
        )
    with pytest.raises(locks.LockGenerationError):
        locks.require_supported_runtime(
            platform_name="win32",
            implementation="CPython",
            python_version=(3, 14),
            machine="AMD64",
        )


def test_report_is_rendered_as_pip_hash_lock() -> None:
    locks = load_lock_module()
    report = sample_report()

    locks.validate_report_environment(report)
    distributions = locks.distributions_from_report(report)
    content = locks.render_lock(locks.LOCK_PROFILES[0], distributions)

    assert distributions == (
        locks.LockedDistribution(name="pillow", version="12.2.0", sha256="a" * 64),
    )
    assert "--only-binary=:all:\n--require-hashes" in content
    assert "pillow==12.2.0 \\\n    --hash=sha256:" + ("a" * 64) in content
    assert "bildebank==0.9.0" not in content


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"url": "https://files.pythonhosted.org/packages/Pillow-12.2.0.tar.gz"}, "binærhjul"),
        ({"url": "http://files.pythonhosted.org/packages/Pillow-12.2.0-cp313-win_amd64.whl"}, "HTTPS"),
        ({"sha256": "ikke-en-hash"}, "ugyldig SHA-256"),
    ],
)
def test_report_rejects_unsafe_distribution(change: dict[str, str], error: str) -> None:
    locks = load_lock_module()
    report = sample_report(**change)

    with pytest.raises(locks.LockGenerationError, match=error):
        locks.distributions_from_report(report)


def test_report_rejects_yanked_distribution() -> None:
    locks = load_lock_module()
    report = sample_report()
    install = report["install"]
    assert isinstance(install, list)
    package = install[1]
    assert isinstance(package, dict)
    package["is_yanked"] = True

    with pytest.raises(locks.LockGenerationError, match="trukket tilbake"):
        locks.distributions_from_report(report)


def test_all_profiles_are_validated_before_lock_files_are_replaced(tmp_path: Path) -> None:
    locks = load_lock_module()
    lock_directory = tmp_path / "requirements"
    lock_directory.mkdir()
    existing = lock_directory / locks.LOCK_PROFILES[0].filename
    existing.write_text("gammel lås\n", encoding="utf-8")
    calls = 0

    def resolve(profile, repo_root, temp_directory):
        nonlocal calls
        del profile, repo_root, temp_directory
        calls += 1
        if calls == 2:
            return {"version": "ukjent"}
        return sample_report()

    with pytest.raises(locks.LockGenerationError, match="rapportversjon"):
        locks.generate_dependency_locks(
            repo_root=tmp_path,
            lock_directory=lock_directory,
            resolve=resolve,
        )

    assert existing.read_text(encoding="utf-8") == "gammel lås\n"
    assert list(lock_directory.iterdir()) == [existing]


def test_generator_writes_three_separate_full_locks(tmp_path: Path) -> None:
    locks = load_lock_module()
    seen_requirements: list[str] = []

    def resolve(profile, repo_root, temp_directory):
        del repo_root, temp_directory
        seen_requirements.append(profile.project_requirement)
        return sample_report()

    generated = locks.generate_dependency_locks(
        repo_root=tmp_path,
        lock_directory=tmp_path / "requirements",
        resolve=resolve,
    )

    assert seen_requirements == [".", ".[face]", ".[openclip]"]
    assert [path.name for path in generated] == [
        "windows-py313-base.lock",
        "windows-py313-face.lock",
        "windows-py313-openclip.lock",
    ]
    assert all(path.read_text(encoding="utf-8").count("pillow==12.2.0") == 1 for path in generated)
