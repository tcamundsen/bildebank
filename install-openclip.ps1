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
$ModelRoot = Join-Path $RepoDir ".bildebank-openclip"
$ModelName = "ViT-B-32"
$Pretrained = "laion2b_s34b_b79k"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Fant ikke Python i .venv. Installer Bildebank forst."
}

Push-Location $RepoDir
try {
    Write-Host "Installerer valgfri OpenCLIP-stotte i Bildebanks lokale Python-miljo"
    Invoke-Native -FilePath $VenvPython -ArgumentList @("-m", "pip", "install", "-e", ".[openclip]")

    New-Item -ItemType Directory -Force -Path $ModelRoot | Out-Null

    $SmokeTest = New-TemporaryFile
    Set-Content -LiteralPath $SmokeTest -Encoding UTF8 -Value @'
import sys
from pathlib import Path

import open_clip

model_root = Path(sys.argv[1])
model_name = sys.argv[2]
pretrained = sys.argv[3]

model_root.mkdir(parents=True, exist_ok=True)
open_clip.create_model_and_transforms(
    model_name,
    pretrained=pretrained,
    device="cpu",
    cache_dir=str(model_root),
)
open_clip.get_tokenizer(model_name)
print(f"OpenCLIP klar: {model_name} ({pretrained})")
'@

    try {
        Write-Host "Laster ned og tester OpenCLIP-modell:"
        Write-Host "  $ModelName ($Pretrained)"
        Write-Host "Modellmappe:"
        Write-Host "  $ModelRoot"
        Invoke-Native -FilePath $VenvPython -ArgumentList @($SmokeTest.FullName, $ModelRoot, $ModelName, $Pretrained)
    } finally {
        Remove-Item -LiteralPath $SmokeTest -Force -ErrorAction SilentlyContinue
    }

    Write-Host "Ferdig. OpenCLIP er installert med modell:"
    Write-Host "  $ModelName ($Pretrained)"
} finally {
    Pop-Location
}
