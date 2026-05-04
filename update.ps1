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
    Invoke-Native -FilePath $venvPython -ArgumentList @("-m", "pip", "install", "-e", ".")
} finally {
    Pop-Location
}

Write-Step "Ferdig"
Write-Host "Test gjerne:"
Write-Host "  bildebank --help"
