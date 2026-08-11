$ErrorActionPreference = "Stop"
$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $Utf8NoBom
[Console]::OutputEncoding = $Utf8NoBom
$OutputEncoding = $Utf8NoBom
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

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

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Fant ikke Python i .venv. Installer Bildebank først."
}

Push-Location $RepoDir
try {
    Write-Host "Installerer valgfri OpenCLIP-støtte i Bildebanks lokale Python-miljø"
    $DependencyLock = Join-Path $RepoDir "requirements\windows-py313-openclip.lock"
    if (-not (Test-Path -LiteralPath $DependencyLock)) {
        throw "Installasjonen mangler dependency-lockfilen: $DependencyLock"
    }
    Invoke-Native -FilePath $VenvPython -ArgumentList @(
        "-m",
        "pip",
        "install",
        "--require-hashes",
        "--only-binary=:all:",
        "-r",
        $DependencyLock
    )

    Write-Host "Kontrollerer OpenCLIP-avhengighetene"
    # Windows PowerShell 5.1 can corrupt embedded double quotes when a
    # multiline string is passed to a native executable as one -c argument.
    $SmokeTest = "import igraph; import open_clip; import sklearn; import torch; from importlib.metadata import version; print('OpenCLIP klar: open_clip_torch=' + version('open_clip_torch') + ', torch=' + torch.__version__ + ', scikit-learn=' + sklearn.__version__ + ', igraph=' + igraph.__version__)"
    Invoke-Native -FilePath $VenvPython -ArgumentList @("-c", $SmokeTest)
    Write-Host "Ferdig. OpenCLIP-avhengighetene er installert."
    Write-Host "Last ned valgt modell separat fra Oppsett-fanen."
} finally {
    Pop-Location
}
