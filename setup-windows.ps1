param(
    [string]$RepoUrl = "https://github.com/tcamundsen/bildebank.git",
    [string]$InstallDir = (Join-Path $HOME "kode\bildebank"),
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "== $Message =="
}

function Test-Command {
    param([string]$Name)
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Refresh-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
}

function Ensure-WingetPackage {
    param(
        [string]$PackageId,
        [string]$Name
    )

    if ($SkipInstall) {
        throw "$Name mangler. Installer $Name manuelt, eller kjør setup uten -SkipInstall."
    }
    if (-not (Test-Command "winget")) {
        throw "$Name mangler, og winget finnes ikke. Installer $Name manuelt og kjør setup på nytt."
    }

    Write-Step "Installerer $Name med winget"
    winget install --id $PackageId --exact --source winget --accept-package-agreements --accept-source-agreements
    Refresh-ProcessPath
}

function Ensure-Git {
    if (Test-Command "git") {
        Write-Host "Git finnes allerede: $(git --version)"
        return
    }
    Ensure-WingetPackage -PackageId "Git.Git" -Name "Git for Windows"
    if (-not (Test-Command "git")) {
        throw "Git ble installert, men finnes ikke i PATH ennå. Lukk PowerShell og kjør setup på nytt."
    }
}

function Test-Python313 {
    if (-not (Test-Command "py")) {
        return $false
    }
    try {
        & py -3.13 --version *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Ensure-Python {
    if (Test-Python313) {
        Write-Host "Python 3.13 finnes allerede: $(py -3.13 --version)"
        return
    }
    Ensure-WingetPackage -PackageId "Python.Python.3.13" -Name "Python 3.13"
    if (-not (Test-Python313)) {
        throw "Python 3.13 ble installert, men py -3.13 virker ikke ennå. Lukk PowerShell og kjør setup på nytt."
    }
}

function Get-RepoDir {
    $scriptDir = $PSScriptRoot
    if ($scriptDir -and (Test-Path -LiteralPath (Join-Path $scriptDir "pyproject.toml"))) {
        return (Resolve-Path -LiteralPath $scriptDir).Path
    }
    return $InstallDir
}

function Ensure-Repo {
    param([string]$RepoDir)

    if (Test-Path -LiteralPath (Join-Path $RepoDir ".git")) {
        Write-Step "Oppdaterer eksisterende repo"
        Push-Location $RepoDir
        try {
            git pull --ff-only
        } finally {
            Pop-Location
        }
        return
    }

    if (Test-Path -LiteralPath $RepoDir) {
        $children = Get-ChildItem -LiteralPath $RepoDir -Force
        if ($children.Count -gt 0) {
            throw "Installasjonsmappen finnes, men er ikke et tomt git-repo: $RepoDir"
        }
    } else {
        New-Item -ItemType Directory -Path (Split-Path -Parent $RepoDir) -Force | Out-Null
    }

    Write-Step "Laster ned bildebank fra GitHub"
    git clone $RepoUrl $RepoDir
}

function Ensure-Venv {
    param([string]$RepoDir)

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

    Write-Step "Installerer bildebank i Python-miljoet"
    Push-Location $RepoDir
    try {
        & $venvPython -m pip install -e .
    } finally {
        Pop-Location
    }
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

Write-Step "Sjekker Git og Python"
Ensure-Git
Ensure-Python

$repoDir = Get-RepoDir
Ensure-Repo -RepoDir $repoDir
Ensure-Venv -RepoDir $repoDir

$binDir = Join-Path $repoDir "bin"
Add-ToUserPath -Directory $binDir
Warn-CommandCollision -ExpectedBinDir $binDir

Write-Step "Ferdig"
Write-Host "Programmet ligger i: $repoDir"
Write-Host "Start en ny PowerShell og test:"
Write-Host "  bildebank --help"
Write-Host ""
Write-Host "Hvis du vil oppdatere senere:"
Write-Host "  cd `"$repoDir`""
Write-Host "  .\update.ps1"
