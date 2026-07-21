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

function Remove-LegacyPythonMetadata {
    param([string]$RepoDir)

    Remove-Item -LiteralPath (Join-Path $RepoDir "bilder.egg-info") -Recurse -Force -ErrorAction SilentlyContinue
}

if (-not (Test-Path -LiteralPath (Join-Path $RepoDir ".git"))) {
    throw "Fant ikke git-repo: $RepoDir"
}

if (-not (Test-Path -LiteralPath (Join-Path $RepoDir "pyproject.toml"))) {
    throw "Fant ikke pyproject.toml i: $RepoDir"
}

Write-Step "Henter oppdateringer"
Push-Location $RepoDir
try {
    Invoke-Native -FilePath "git" -ArgumentList @("pull", "--ff-only")
} finally {
    Pop-Location
}

$venvPython = Join-Path $RepoDir ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Step "Lager Python-miljo"
    Push-Location $RepoDir
    try {
        Invoke-Native -FilePath "py" -ArgumentList @("-3.13", "-m", "venv", ".venv")
    } finally {
        Pop-Location
    }
}

Write-Step "Oppdaterer Python-installasjon"
Push-Location $RepoDir
try {
    Remove-LegacyPythonMetadata -RepoDir $RepoDir
    Invoke-Native -FilePath $venvPython -ArgumentList @("-m", "pip", "install", "-e", ".")
} finally {
    Pop-Location
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
