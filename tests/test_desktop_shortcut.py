from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell.exe")
requires_powershell = pytest.mark.skipif(not POWERSHELL, reason="Requires PowerShell")

MOCK_SHELL = """
function New-Object {
    param([string]$ComObject)
    if ($ComObject -ne 'WScript.Shell') { throw 'Unexpected COM object' }
    $folders = [pscustomobject]@{}
    Add-Member -InputObject $folders -MemberType ScriptMethod -Name Item -Value {
        param($name)
        if ($name -ne 'Desktop') { throw 'Unexpected special folder' }
        return $env:BILDEBANK_TEST_DESKTOP
    }
    $shell = [pscustomobject]@{ SpecialFolders = $folders }
    Add-Member -InputObject $shell -MemberType ScriptMethod -Name CreateShortcut -Value {
        param($path)
        $shortcut = [pscustomobject]@{
            TargetPath = ''; WorkingDirectory = ''; Description = ''; OutputPath = $path
        }
        Add-Member -InputObject $shortcut -MemberType ScriptMethod -Name Save -Value {
            [IO.File]::WriteAllText($this.OutputPath, ($this | ConvertTo-Json))
        }
        return $shortcut
    }
    return $shell
}
"""


def run_powershell(tmp_path: Path, body: str, **env: str) -> subprocess.CompletedProcess[str]:
    wrapper = tmp_path / "test-shortcut.ps1"
    wrapper.write_text("$ErrorActionPreference = 'Stop'\n" + body, encoding="utf-8-sig")
    return subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(wrapper)],
        cwd=tmp_path,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@requires_powershell
@pytest.mark.parametrize("existing", [False, True])
def test_shortcut_uses_windows_desktop_and_preserves_existing_file(
    tmp_path: Path, existing: bool
) -> None:
    desktop = tmp_path / "OneDrive [privat]" / "Skrivebord"
    desktop.mkdir(parents=True)
    program = tmp_path / "program med æ og mellomrom"
    program.mkdir()
    start_script = program / "start-bildebank.cmd"
    start_script.touch()
    shortcut = desktop / "Bildebank.lnk"
    if existing:
        shortcut.write_bytes(b"existing user shortcut")

    result = run_powershell(
        tmp_path,
        MOCK_SHELL + '& $env:BILDEBANK_TEST_SCRIPT -RepoDir $env:BILDEBANK_TEST_REPO\n',
        BILDEBANK_TEST_SCRIPT=str(REPO_ROOT / "create-desktop-shortcut.ps1"),
        BILDEBANK_TEST_REPO=str(program),
        BILDEBANK_TEST_DESKTOP=str(desktop),
    )

    assert result.returncode == 0, result.stderr
    if existing:
        assert shortcut.read_bytes() == b"existing user shortcut"
    else:
        saved = json.loads(shortcut.read_text(encoding="utf-8-sig"))
        assert saved["TargetPath"] == str(start_script)
        assert saved["WorkingDirectory"] == str(program)
        assert saved["Description"] == "Start Bildebank"


@requires_powershell
@pytest.mark.parametrize("missing", ["desktop", "start-script"])
def test_missing_desktop_or_start_script_does_not_create_shortcut(
    tmp_path: Path, missing: str
) -> None:
    desktop = tmp_path / "desktop"
    program = tmp_path / "program"
    program.mkdir()
    if missing != "desktop":
        desktop.mkdir()
    if missing != "start-script":
        (program / "start-bildebank.cmd").touch()

    result = run_powershell(
        tmp_path,
        MOCK_SHELL + '& $env:BILDEBANK_TEST_SCRIPT -RepoDir $env:BILDEBANK_TEST_REPO\n',
        BILDEBANK_TEST_SCRIPT=str(REPO_ROOT / "create-desktop-shortcut.ps1"),
        BILDEBANK_TEST_REPO=str(program),
        BILDEBANK_TEST_DESKTOP=str(desktop),
    )

    assert result.returncode != 0
    assert not (desktop / "Bildebank.lnk").exists()


@requires_powershell
@pytest.mark.parametrize("script_name", ["setup-windows.ps1", "update.ps1"])
def test_shortcut_failure_does_not_fail_completed_install_or_update(
    tmp_path: Path, script_name: str
) -> None:
    script = (REPO_ROOT / script_name).read_text(encoding="utf-8-sig")
    # Run the actual post-install block with the helper deliberately missing.
    start = script.rindex('\ntry {\n    & (Join-Path')
    end = script.rindex('Write-Step "Ferdig"')
    result = run_powershell(
        tmp_path,
        "$RepoDir = $env:BILDEBANK_TEST_REPO\n" + script[start:end],
        BILDEBANK_TEST_REPO=str(tmp_path),
    )

    assert result.returncode == 0, result.stderr
    assert "Kunne ikke lage skrivebordssnarvei" in result.stdout
    assert "start-bildebank.cmd" in result.stdout


@pytest.mark.parametrize("script_name", ["setup-windows.ps1", "update.ps1"])
def test_shortcut_is_created_after_successful_install_or_update(script_name: str) -> None:
    script = (REPO_ROOT / script_name).read_text(encoding="utf-8-sig")
    call = script.index('"create-desktop-shortcut.ps1"')
    if script_name == "setup-windows.ps1":
        assert call > script.index('throw "Installasjonen feilet: $setupError"')
    else:
        assert call > script.rindex("Remove-UpdateState")
        assert call > script.index('"Ruller tilbake oppdateringen"')
    assert call < script.rindex('Write-Step "Ferdig"')
