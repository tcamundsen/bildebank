from __future__ import annotations

from pathlib import Path


def read_setup_script() -> str:
    return (
        Path(__file__).resolve().parents[1] / "setup-windows.ps1"
    ).read_text(encoding="utf-8")


def test_new_windows_install_uses_validated_sibling_staging_and_rollback() -> None:
    script = read_setup_script()

    assert "Clone-NewRepoToStaging" in script
    assert '-Purpose "setup-staging"' in script
    assert "-StagingDir $stagingDir" in script
    assert "Assert-ValidBildebankCheckout" in script
    assert "Publish-NewRepo" in script
    assert "Restore-NewRepoAfterFailure" in script
    assert '-Purpose "setup-failed"' in script
    assert "Ufullstendig staging er bevart her" in script
    assert "Move-Item -LiteralPath $StagingDir -Destination $RepoDir" in script
    assert '"--single-branch"' in script
    assert '"--",' in script


def test_existing_windows_install_is_verified_before_update_script_runs() -> None:
    script = read_setup_script()

    origin_check = script.index("Assert-ExpectedOrigin -RepoDir")
    clean_check = script.index("Assert-CleanRepo -RepoDir")
    update_call = script.index("& $updateScript -RepoDir $RepoDir")
    assert origin_check < update_call
    assert clean_check < update_call
    assert '"@{upstream}"' in script
    assert "Bruk en egen InstallDir for en annen branch." in script


def test_windows_setup_rejects_reparse_points_and_checks_python_platform() -> None:
    script = read_setup_script()

    assert "[IO.FileAttributes]::ReparsePoint" in script
    assert "Assert-PlainDirectory" in script
    assert "Assert-PlainFile" in script
    assert "sys.implementation.name == 'cpython'" in script
    assert "sys.version_info[:2] == (3, 13)" in script
    assert "struct.calcsize('P') == 8" in script
    assert "platform.machine().lower() in ('amd64', 'x86_64')" in script
    assert "bildebank-tools\\setup-smoke" in script
    assert "actual != expected" in script


def test_custom_command_shim_is_published_via_unique_temp_file() -> None:
    script = read_setup_script()

    assert ".$CommandName.cmd.setup-" in script
    assert "Copy-Item -LiteralPath $defaultShim -Destination $tempShim" in script
    assert (
        "Move-Item -LiteralPath $tempShim -Destination $commandShim -Force"
        in script
    )
