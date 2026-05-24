$ErrorActionPreference = "Stop"

function Invoke-Native {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList
    )
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "Kommando feilet ($LASTEXITCODE): $FilePath $($ArgumentList -join ' ')"
    }
}

$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $RepoDir ".venv\Scripts\python.exe"
$ConfigExample = Join-Path $RepoDir "bildebank-config.example.toml"
$ConfigFile = Join-Path $RepoDir "bildebank-config.toml"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Fant ikke Python i .venv. Installer Bildebank først."
}

Push-Location $RepoDir
try {
    Write-Host "Installerer valgfri InsightFace-stotte i Bildebanks lokale Python-miljo"
    # .[face] betyr optional-dependencies face, som listet i pyprosject.toml
    Invoke-Native -FilePath $VenvPython -ArgumentList @("-m", "pip", "install", "-e", ".[face]")

    if (-not (Test-Path -LiteralPath $ConfigFile)) {
        Copy-Item -LiteralPath $ConfigExample -Destination $ConfigFile
        Write-Host "Opprettet config-fil:"
        Write-Host "  $ConfigFile"
        Write-Host "Endre enabled = true hvis du vil sla pa testing senere."
    }

    Write-Host "Ferdig. Sjekk status med:"
    Write-Host "  bildebank doctor"
} finally {
    Pop-Location
}
