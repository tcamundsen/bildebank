param(
    [string]$RepoDir = $PSScriptRoot
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "== $Message =="
}

function Invoke-Native {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        $command = "$FilePath $($ArgumentList -join ' ')"
        throw "Kommando feilet med exit code ${LASTEXITCODE}: $command"
    }
}

function Invoke-GitCapture {
    param([string[]]$ArgumentList)

    $output = & git -C $RepoDir @ArgumentList 2>&1
    if ($LASTEXITCODE -ne 0) {
        $command = "git -C $RepoDir $($ArgumentList -join ' ')"
        $details = (($output | ForEach-Object { $_.ToString() }) -join "`n").Trim()
        if ($details) {
            throw "Kommando feilet med exit code ${LASTEXITCODE}: ${command}: $details"
        }
        throw "Kommando feilet med exit code ${LASTEXITCODE}: $command"
    }
    return (($output | ForEach-Object { $_.ToString() }) -join "`n").Trim()
}

function Remove-LegacyPythonMetadata {
    param([string]$RepoDir)

    Remove-Item -LiteralPath (Join-Path $RepoDir "bilder.egg-info") -Recurse -Force -ErrorAction SilentlyContinue
}

function Assert-CleanRepo {
    $status = Invoke-GitCapture -ArgumentList @(
        "status",
        "--porcelain=v1",
        "--untracked-files=no"
    )
    if ($status) {
        throw (
            "Programrepoet har lokale endringer i Git-sporede filer. " +
            "Commit eller tilbakestill dem før oppdatering:`n$status"
        )
    }
}

function Ensure-PythonEnvironment {
    if (Test-Path -LiteralPath $venvPython) {
        return
    }
    Write-Step "Lager Python-miljo"
    Push-Location $RepoDir
    try {
        Invoke-Native -FilePath "py" -ArgumentList @("-3.13", "-m", "venv", ".venv")
    } finally {
        Pop-Location
    }
}

function Install-And-Test {
    param([switch]$NoDependencies)

    Write-Step "Oppdaterer Python-installasjon"
    Push-Location $RepoDir
    try {
        Remove-LegacyPythonMetadata -RepoDir $RepoDir
        $pipArguments = @("-m", "pip", "install")
        if ($NoDependencies) {
            $pipArguments += @("--no-deps", "--no-build-isolation")
        }
        $pipArguments += @("-e", ".")
        Invoke-Native -FilePath $venvPython -ArgumentList $pipArguments
    } finally {
        Pop-Location
    }
    New-Item -ItemType Directory -Path $updateSmokeDirectory -Force | Out-Null
    Push-Location $updateSmokeDirectory
    try {
        Invoke-Native -FilePath $venvPython -ArgumentList @(
            "-c",
            "from bildebank.cli import main"
        )
    } finally {
        Pop-Location
    }
}

function Write-UpdateState {
    param([string]$OldCommit)

    if ($OldCommit -notmatch "\A[0-9a-fA-F]{40,64}\z") {
        throw "Git ga en ugyldig commit-ID: $OldCommit"
    }
    $stateDirectory = Split-Path -Parent $updateStatePath
    New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
    $tempPath = "$updateStatePath.tmp-$([Guid]::NewGuid().ToString('N'))"
    try {
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($tempPath, "$OldCommit`n", $utf8NoBom)
        Move-Item -LiteralPath $tempPath -Destination $updateStatePath
    } finally {
        Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    }
}

function Read-UpdateState {
    try {
        $oldCommit = (Get-Content -LiteralPath $updateStatePath -Raw).Trim()
    } catch {
        throw "Kunne ikke lese recovery-markoren ${updateStatePath}: $($_.Exception.Message)"
    }
    if ($oldCommit -notmatch "\A[0-9a-fA-F]{40,64}\z") {
        throw "Recovery-markoren har en ugyldig commit-ID: $oldCommit"
    }
    Push-Location $RepoDir
    try {
        Invoke-Native -FilePath "git" -ArgumentList @(
            "cat-file",
            "-e",
            "$($oldCommit)^{commit}"
        )
    } finally {
        Pop-Location
    }
    return $oldCommit
}

function Remove-UpdateState {
    if (Test-Path -LiteralPath $updateStatePath) {
        Remove-Item -LiteralPath $updateStatePath -Force
    }
}

function Restore-PreviousVersion {
    param([string]$OldCommit)

    Assert-CleanRepo
    Push-Location $RepoDir
    try {
        Invoke-Native -FilePath "git" -ArgumentList @("reset", "--hard", $OldCommit)
    } finally {
        Pop-Location
    }
    Ensure-PythonEnvironment
    Install-And-Test -NoDependencies
    Remove-UpdateState
}

if (-not (Test-Path -LiteralPath (Join-Path $RepoDir ".git"))) {
    throw "Fant ikke git-repo: $RepoDir"
}

if (-not (Test-Path -LiteralPath (Join-Path $RepoDir "pyproject.toml"))) {
    throw "Fant ikke pyproject.toml i: $RepoDir"
}

$venvPython = Join-Path $RepoDir ".venv\Scripts\python.exe"
$updateStatePath = Join-Path $RepoDir "bildebank-tools\update-pending.txt"
$updateSmokeDirectory = Join-Path $RepoDir "bildebank-tools\update-smoke"

if (Test-Path -LiteralPath $updateStatePath) {
    Write-Step "Gjenoppretter avbrutt oppdatering"
    $oldCommit = Read-UpdateState
    try {
        Restore-PreviousVersion -OldCommit $oldCommit
    } catch {
        throw (
            "Fant en avbrutt oppdatering, men klarte ikke a gjenopprette " +
            "forrige versjon. Recovery-markoren er beholdt: " +
            "${updateStatePath}: $($_.Exception.Message)"
        )
    }
    throw (
        "Forrige oppdatering ble avbrutt. Den gamle versjonen er " +
        "gjenopprettet og kontrollert. Kjor bildebank update pa nytt."
    )
}

Assert-CleanRepo
Ensure-PythonEnvironment
$oldCommit = Invoke-GitCapture -ArgumentList @("rev-parse", "--verify", "HEAD")
Write-UpdateState -OldCommit $oldCommit

try {
    Write-Step "Henter oppdateringer"
    Push-Location $RepoDir
    try {
        Invoke-Native -FilePath "git" -ArgumentList @("pull", "--ff-only")
    } finally {
        Pop-Location
    }
    Install-And-Test
    Remove-UpdateState
} catch {
    $updateError = $_.Exception.Message
    Write-Step "Ruller tilbake oppdateringen"
    try {
        Restore-PreviousVersion -OldCommit $oldCommit
    } catch {
        throw (
            "Oppdateringen feilet, og automatisk rollback feilet ogsa. " +
            "Recovery-markoren er beholdt: ${updateStatePath}. " +
            "Oppdateringsfeil: ${updateError}. " +
            "Rollback-feil: $($_.Exception.Message)"
        )
    }
    throw (
        "Oppdateringen feilet. Den gamle versjonen er gjenopprettet og " +
        "kontrollert. Opprinnelig feil: $updateError"
    )
}

Write-Step "Kontrollerer FFmpeg"
Push-Location $RepoDir
try {
    try {
        Invoke-Native -FilePath $venvPython -ArgumentList @("-m", "bildebank", "ffmpeg-install")
    } catch {
        Write-Host "Kunne ikke installere FFmpeg automatisk: $($_.Exception.Message)"
        Write-Host "Bildebank er oppdatert og vil prøve igjen ved neste oppstart."
    }
} finally {
    Pop-Location
}

Write-Step "Ferdig"
Write-Host "Test gjerne:"
Write-Host "  bildebank --help"
