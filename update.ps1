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

function Assert-UpdateProfiles {
    param([string[]]$Profiles = @())

    $knownProfiles = @("face", "openclip")
    $seenProfiles = @{}
    foreach ($profile in @($Profiles)) {
        if ($profile -notin $knownProfiles) {
            throw "Recovery-markoren har en ukjent installasjonsprofil: $profile"
        }
        if ($seenProfiles.ContainsKey($profile)) {
            throw "Recovery-markoren har duplisert installasjonsprofil: $profile"
        }
        $seenProfiles[$profile] = $true
    }
    foreach ($profile in $knownProfiles) {
        if ($seenProfiles.ContainsKey($profile)) {
            Write-Output $profile
        }
    }
}

function Get-InstalledProfiles {
    $profiles = @()
    $profileModules = [ordered]@{
        "face" = "insightface"
        "openclip" = "open_clip"
    }
    foreach ($entry in $profileModules.GetEnumerator()) {
        & $venvPython -c (
            "import importlib.util, sys; " +
            "sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)"
        ) $entry.Value
        $probeExitCode = $LASTEXITCODE
        if ($probeExitCode -eq 0) {
            $profiles += $entry.Key
        } elseif ($probeExitCode -ne 1) {
            throw (
                "Kunne ikke kontrollere installert profil $($entry.Key): " +
                "Python avsluttet med kode $probeExitCode."
            )
        }
    }
    return $profiles
}

function Install-DependencyLock {
    param([string]$LockName)

    $dependencyLock = Join-Path $RepoDir "requirements\$LockName"
    if (-not (Test-Path -LiteralPath $dependencyLock)) {
        throw "Oppdateringen mangler dependency-lockfilen: $dependencyLock"
    }
    Invoke-Native -FilePath $venvPython -ArgumentList @(
        "-m",
        "pip",
        "install",
        "--require-hashes",
        "--only-binary=:all:",
        "-r",
        $dependencyLock
    )
}

function Install-And-Test {
    param([string[]]$Profiles = @())

    $Profiles = @(Assert-UpdateProfiles -Profiles $Profiles)
    Write-Step "Oppdaterer Python-installasjon"
    Push-Location $RepoDir
    try {
        Remove-LegacyPythonMetadata -RepoDir $RepoDir
        Install-DependencyLock -LockName "windows-py313-base.lock"
        foreach ($profile in $Profiles) {
            $lockName = switch ($profile) {
                "face" { "windows-py313-face.lock" }
                "openclip" { "windows-py313-openclip.lock" }
            }
            Install-DependencyLock -LockName $lockName
        }
        Invoke-Native -FilePath $venvPython -ArgumentList @(
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-build-isolation",
            "-e",
            "."
        )
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
        foreach ($profile in $Profiles) {
            $smokeTest = switch ($profile) {
                "face" { "import insightface; import onnxruntime" }
                "openclip" { "import open_clip; import torch" }
            }
            Invoke-Native -FilePath $venvPython -ArgumentList @("-c", $smokeTest)
        }
    } finally {
        Pop-Location
    }
}

function Write-UpdateState {
    param(
        [string]$OldCommit,
        [string[]]$Profiles = @()
    )

    if ($OldCommit -notmatch "\A[0-9a-fA-F]{40,64}\z") {
        throw "Git ga en ugyldig commit-ID: $OldCommit"
    }
    $Profiles = @(Assert-UpdateProfiles -Profiles $Profiles)
    $payload = [ordered]@{
        version = 1
        old_commit = $OldCommit
        profiles = @($Profiles)
    } | ConvertTo-Json -Compress
    $stateDirectory = Split-Path -Parent $updateStatePath
    New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
    $tempPath = "$updateStatePath.tmp-$([Guid]::NewGuid().ToString('N'))"
    try {
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($tempPath, "$payload`n", $utf8NoBom)
        Move-Item -LiteralPath $tempPath -Destination $updateStatePath
    } finally {
        Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    }
}

function Read-UpdateState {
    try {
        $rawState = (Get-Content -LiteralPath $updateStatePath -Raw).Trim()
    } catch {
        throw "Kunne ikke lese recovery-markoren ${updateStatePath}: $($_.Exception.Message)"
    }
    if ($rawState -match "\A[0-9a-fA-F]{40,64}\z") {
        $oldCommit = $rawState
        $profiles = @()
        $legacy = $true
    } else {
        try {
            $payload = $rawState | ConvertFrom-Json
        } catch {
            throw "Recovery-markoren har ugyldig format: $updateStatePath"
        }
        $propertyNames = @($payload.PSObject.Properties.Name)
        if (
            $payload.version -ne 1 -or
            "old_commit" -notin $propertyNames -or
            "profiles" -notin $propertyNames -or
            $payload.old_commit -isnot [string]
        ) {
            throw "Recovery-markoren har ugyldig format: $updateStatePath"
        }
        $oldCommit = $payload.old_commit
        $profiles = @(Assert-UpdateProfiles -Profiles @($payload.profiles))
        $legacy = $false
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
    return [PSCustomObject]@{
        OldCommit = $oldCommit
        Profiles = @($profiles)
        Legacy = $legacy
    }
}

function Remove-UpdateState {
    if (Test-Path -LiteralPath $updateStatePath) {
        Remove-Item -LiteralPath $updateStatePath -Force
    }
}

function Restore-PreviousVersion {
    param(
        [string]$OldCommit,
        [string[]]$Profiles = @()
    )

    Assert-CleanRepo
    Push-Location $RepoDir
    try {
        Invoke-Native -FilePath "git" -ArgumentList @("reset", "--hard", $OldCommit)
    } finally {
        Pop-Location
    }
    Ensure-PythonEnvironment
    Install-And-Test -Profiles $Profiles
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
    $state = Read-UpdateState
    if ($state.Legacy) {
        Ensure-PythonEnvironment
        $state.Profiles = @(Get-InstalledProfiles)
    }
    try {
        Restore-PreviousVersion `
            -OldCommit $state.OldCommit `
            -Profiles $state.Profiles
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
$installedProfiles = @(Get-InstalledProfiles)
$oldCommit = Invoke-GitCapture -ArgumentList @("rev-parse", "--verify", "HEAD")
Write-UpdateState -OldCommit $oldCommit -Profiles $installedProfiles

try {
    Write-Step "Henter oppdateringer"
    Push-Location $RepoDir
    try {
        Invoke-Native -FilePath "git" -ArgumentList @("pull", "--ff-only")
    } finally {
        Pop-Location
    }
    Install-And-Test -Profiles $installedProfiles
    Remove-UpdateState
} catch {
    $updateError = $_.Exception.Message
    Write-Step "Ruller tilbake oppdateringen"
    try {
        Restore-PreviousVersion `
            -OldCommit $oldCommit `
            -Profiles $installedProfiles
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
