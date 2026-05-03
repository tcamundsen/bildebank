param(
    [string]$RepoDir = $PSScriptRoot
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "== $Message =="
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
    git pull --ff-only
} finally {
    Pop-Location
}

$venvPython = Join-Path $RepoDir ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Step "Lager Python-miljo"
    Push-Location $RepoDir
    try {
        py -3.13 -m venv .venv
    } finally {
        Pop-Location
    }
}

Write-Step "Oppdaterer Python-installasjon"
Push-Location $RepoDir
try {
    & $venvPython -m pip install -e .
} finally {
    Pop-Location
}

Write-Step "Ferdig"
Write-Host "Test gjerne:"
Write-Host "  bildebank --help"
