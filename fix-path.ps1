param(
    [string]$RepoDir = $PSScriptRoot
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "== $Message =="
}

function Refresh-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
}

function Add-ToUserPath {
    param([string]$Directory)

    $resolved = (Resolve-Path -LiteralPath $Directory).Path
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @()
    if ($userPath) {
        $parts = $userPath -split ";" | Where-Object { $_ -ne "" }
    }

    $alreadyPresent = $false
    foreach ($part in $parts) {
        if ([string]::Equals($part.TrimEnd("\"), $resolved.TrimEnd("\"), [StringComparison]::OrdinalIgnoreCase)) {
            $alreadyPresent = $true
            break
        }
    }

    if (-not $alreadyPresent) {
        Write-Step "Legger bildebank i bruker-PATH"
        $newPath = if ($userPath) { "$userPath;$resolved" } else { $resolved }
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        Refresh-ProcessPath
        Write-Host "La til: $resolved"
    } else {
        Write-Host "PATH inneholder allerede: $resolved"
    }
}

function Warn-CommandCollision {
    param([string]$ExpectedBinDir)

    $command = Get-Command "bildebank" -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        return
    }
    $expected = (Resolve-Path -LiteralPath $ExpectedBinDir).Path
    $actual = Split-Path -Parent $command.Source
    if (-not [string]::Equals($actual.TrimEnd("\"), $expected.TrimEnd("\"), [StringComparison]::OrdinalIgnoreCase)) {
        Write-Host "Advarsel: 'bildebank' finnes allerede her: $($command.Source)"
        Write-Host "Hvis feil kommando starter, flytt $expected tidligere i PATH."
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $RepoDir "pyproject.toml"))) {
    throw "Fant ikke pyproject.toml i: $RepoDir. Kjør scriptet fra programmappen."
}

$binDir = Join-Path $RepoDir "bin"
if (-not (Test-Path -LiteralPath (Join-Path $binDir "bildebank.cmd"))) {
    throw "Fant ikke bin\bildebank.cmd i: $RepoDir"
}

Add-ToUserPath -Directory $binDir
Warn-CommandCollision -ExpectedBinDir $binDir

Write-Step "Ferdig"
Write-Host "Lukk PowerShell og åpne PowerShell på nytt."
Write-Host "Test deretter:"
Write-Host "  bildebank --help"
